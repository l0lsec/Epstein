#!/usr/bin/env bash
# deploy.sh — Deploy code files to Azure VM (never deploys database/data files)
set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
SSH_HOST="REDACTED-HOST"
SSH_USER="sedriclouissaint"
SSH_TARGET="${SSH_USER}@${SSH_HOST}"
REMOTE_DIR="/opt/epstein"
BACKUP_DIR="${REMOTE_DIR}/.deploy-backup"
VENV_PIP="${REMOTE_DIR}/venv/bin/pip"
SERVICE_NAME="epstein"

# Files that require a service restart (Python process must reload)
RESTART_PATTERNS="^backend/|^run\.py$|^requirements\.txt$"

# Blocked file extensions — NEVER deploy these
BLOCKED_EXTENSIONS='\.db$|\.db-wal$|\.db-shm$|\.sqlite$|\.sqlite3$|\.backup$|\.log$|\.pkl$|\.pdf$|\.jpg$|\.tif$|\.mp4$|\.wav$|\.zip$|\.part$|\.env$'

# Blocked directories — NEVER deploy from these
BLOCKED_DIRS='^thumbnails/|^vector_store|^logs/|^mlx_models/|^extracted_text/|^plans/'

# ─── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[SKIP]${NC} $*"; }
error()   { echo -e "${RED}[ERR]${NC}  $*" >&2; }

# ─── Error trap ──────────────────────────────────────────────────────────────
trap 'error "Deploy failed at line $LINENO. Aborting."' ERR

# ─── Flags ───────────────────────────────────────────────────────────────────
DRY_RUN=false
ROLLBACK=false
FORCE_RESTART=false
DEPLOY_ALL=false

for arg in "$@"; do
    case "$arg" in
        --dry-run)       DRY_RUN=true ;;
        --rollback)      ROLLBACK=true ;;
        --force-restart) FORCE_RESTART=true ;;
        --all)           DEPLOY_ALL=true ;;
        -h|--help)
            echo "Usage: ./deploy.sh [--dry-run] [--rollback] [--force-restart] [--all]"
            echo ""
            echo "  --dry-run        Show what would be deployed without doing it"
            echo "  --rollback       Restore the previous deploy backup and restart"
            echo "  --force-restart  Force a service restart even for frontend-only deploys"
            echo "  --all            Deploy all code files regardless of what changed"
            exit 0
            ;;
        *) error "Unknown flag: $arg"; exit 1 ;;
    esac
done

# ─── SSH pre-check ───────────────────────────────────────────────────────────
info "Testing SSH connectivity to ${SSH_TARGET}..."
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "${SSH_TARGET}" "echo ok" &>/dev/null; then
    error "Cannot reach ${SSH_TARGET}. Is the server running?"
    exit 1
fi
success "SSH connection OK"

# ─── Rollback mode ──────────────────────────────────────────────────────────
if [ "$ROLLBACK" = true ]; then
    info "Rolling back to previous deploy backup..."
    ssh "${SSH_TARGET}" bash -s <<'ROLLBACK_EOF'
        REMOTE_DIR="/opt/epstein"
        BACKUP_DIR="${REMOTE_DIR}/.deploy-backup"
        if [ ! -d "$BACKUP_DIR" ]; then
            echo "ERROR: No backup found at $BACKUP_DIR"
            exit 1
        fi
        # Restore each backed-up directory
        for dir in backend frontend scripts; do
            if [ -d "${BACKUP_DIR}/${dir}" ]; then
                sudo cp -f "${BACKUP_DIR}/${dir}/"* "${REMOTE_DIR}/${dir}/"
            fi
        done
        [ -f "${BACKUP_DIR}/run.py" ] && sudo cp -f "${BACKUP_DIR}/run.py" "${REMOTE_DIR}/"
        sudo chown -R user:user "$REMOTE_DIR"
        sudo systemctl restart epstein
        sleep 3
        systemctl is-active epstein
ROLLBACK_EOF
    success "Rollback complete and service restarted"
    exit 0
fi

# ─── Detect changed files ───────────────────────────────────────────────────
info "Detecting changed files..."

CURRENT_SHA=$(git rev-parse HEAD)

if [ "$DEPLOY_ALL" = true ]; then
    info "Deploying ALL code files (--all flag)"
    CHANGED_FILES=$(git ls-files -- backend/ frontend/ scripts/ run.py requirements.txt)
else
    # Fetch the last deployed commit SHA from the server
    LAST_DEPLOYED_SHA=$(ssh "${SSH_TARGET}" "cat ${REMOTE_DIR}/.deploy-sha 2>/dev/null" || true)

    if [ -z "$LAST_DEPLOYED_SHA" ]; then
        info "No previous deploy marker found on server — deploying all code files"
        CHANGED_FILES=$(git ls-files -- backend/ frontend/ scripts/ run.py requirements.txt)
    elif [ "$LAST_DEPLOYED_SHA" = "$CURRENT_SHA" ]; then
        # Same commit — check for uncommitted local changes
        CHANGED_FILES=$(git diff --name-only HEAD 2>/dev/null || true)
        UNTRACKED=$(git ls-files --others --exclude-standard -- backend/ frontend/ scripts/ run.py requirements.txt 2>/dev/null || true)
        if [ -n "$UNTRACKED" ]; then
            CHANGED_FILES=$(printf "%s\n%s" "$CHANGED_FILES" "$UNTRACKED" | sort -u)
        fi
        if [ -z "$CHANGED_FILES" ]; then
            warn "Already deployed commit ${CURRENT_SHA:0:7} and no local changes. Nothing to do."
            exit 0
        fi
    else
        info "Last deployed: ${LAST_DEPLOYED_SHA:0:7} -> Current: ${CURRENT_SHA:0:7}"
        # Diff between last deployed commit and current HEAD + any uncommitted changes
        CHANGED_FILES=$(git diff --name-only "$LAST_DEPLOYED_SHA" HEAD 2>/dev/null || git ls-files -- backend/ frontend/ scripts/ run.py requirements.txt)
        # Also include uncommitted changes on top of HEAD
        UNCOMMITTED=$(git diff --name-only HEAD 2>/dev/null || true)
        UNTRACKED=$(git ls-files --others --exclude-standard -- backend/ frontend/ scripts/ run.py requirements.txt 2>/dev/null || true)
        if [ -n "$UNCOMMITTED" ] || [ -n "$UNTRACKED" ]; then
            CHANGED_FILES=$(printf "%s\n%s\n%s" "$CHANGED_FILES" "$UNCOMMITTED" "$UNTRACKED" | sort -u)
        fi
    fi
fi

# Filter to only files in deployable directories
DEPLOY_FILES=$(echo "$CHANGED_FILES" | grep -E '^(backend/|frontend/|scripts/|run\.py$|requirements\.txt$)' || true)

if [ -z "$DEPLOY_FILES" ]; then
    warn "No deployable files changed. Nothing to do."
    exit 0
fi

echo "$DEPLOY_FILES" | while read -r f; do echo "  $f"; done

# ─── Safety scan: block database/data files ──────────────────────────────────
info "Running safety scan..."
BLOCKED=$(echo "$DEPLOY_FILES" | grep -E "${BLOCKED_EXTENSIONS}|${BLOCKED_DIRS}" || true)
if [ -n "$BLOCKED" ]; then
    error "BLOCKED: The following data/database files would be deployed:"
    echo "$BLOCKED" | while read -r f; do echo -e "  ${RED}$f${NC}"; done
    error "Aborting. NEVER deploy database or data files to production."
    exit 1
fi
success "Safety scan passed — no database or data files"

# ─── Determine what categories changed ───────────────────────────────────────
BACKEND_CHANGED=$(echo "$DEPLOY_FILES" | grep -E '^backend/' || true)
FRONTEND_CHANGED=$(echo "$DEPLOY_FILES" | grep -E '^frontend/' || true)
SCRIPTS_CHANGED=$(echo "$DEPLOY_FILES" | grep -E '^scripts/' || true)
RUNPY_CHANGED=$(echo "$DEPLOY_FILES" | grep -E '^run\.py$' || true)
REQS_CHANGED=$(echo "$DEPLOY_FILES" | grep -E '^requirements\.txt$' || true)

NEEDS_RESTART=false
if echo "$DEPLOY_FILES" | grep -qE "$RESTART_PATTERNS"; then
    NEEDS_RESTART=true
fi
if [ "$FORCE_RESTART" = true ]; then
    NEEDS_RESTART=true
fi

# ─── Cache busting ───────────────────────────────────────────────────────────
CACHE_BUMPED=false
if [ -n "$FRONTEND_CHANGED" ]; then
    # Check if any cacheable frontend files changed (js, css)
    JS_CSS_CHANGED=$(echo "$FRONTEND_CHANGED" | grep -E '\.(js|css)$' || true)
    if [ -n "$JS_CSS_CHANGED" ]; then
        info "Frontend JS/CSS changed — auto-bumping cache versions..."

        # Bump styles.css?v=N in index.html
        if echo "$JS_CSS_CHANGED" | grep -q "styles\.css"; then
            CURRENT_V=$(sed -n 's/.*styles\.css?v=\([0-9]*\).*/\1/p' frontend/index.html | head -1)
            CURRENT_V=${CURRENT_V:-0}
            NEW_V=$((CURRENT_V + 1))
            sed -i.bak "s/styles\.css?v=${CURRENT_V}/styles.css?v=${NEW_V}/" frontend/index.html && rm -f frontend/index.html.bak
            success "Bumped styles.css v=${CURRENT_V} -> v=${NEW_V} in index.html"
            CACHE_BUMPED=true
        fi

        # Bump app.js?v=N in index.html
        if echo "$JS_CSS_CHANGED" | grep -q "app\.js"; then
            CURRENT_V=$(sed -n 's/.*app\.js?v=\([0-9]*\).*/\1/p' frontend/index.html | head -1)
            CURRENT_V=${CURRENT_V:-0}
            NEW_V=$((CURRENT_V + 1))
            sed -i.bak "s/app\.js?v=${CURRENT_V}/app.js?v=${NEW_V}/" frontend/index.html && rm -f frontend/index.html.bak
            success "Bumped app.js v=${CURRENT_V} -> v=${NEW_V} in index.html"
            CACHE_BUMPED=true
        fi

        # Bump admin.js?v=N in admin.html
        if echo "$JS_CSS_CHANGED" | grep -q "admin\.js"; then
            CURRENT_V=$(sed -n 's/.*admin\.js?v=\([0-9]*\).*/\1/p' frontend/admin.html | head -1)
            CURRENT_V=${CURRENT_V:-0}
            NEW_V=$((CURRENT_V + 1))
            sed -i.bak "s/admin\.js?v=${CURRENT_V}/admin.js?v=${NEW_V}/" frontend/admin.html && rm -f frontend/admin.html.bak
            success "Bumped admin.js v=${CURRENT_V} -> v=${NEW_V} in admin.html"
            CACHE_BUMPED=true
        fi
    fi
fi

if [ "$CACHE_BUMPED" = false ] && [ -n "$FRONTEND_CHANGED" ]; then
    warn "Frontend changed but no JS/CSS files — skipping cache bust"
fi

# ─── Dry run stops here ─────────────────────────────────────────────────────
if [ "$DRY_RUN" = true ]; then
    echo ""
    echo -e "${BOLD}=== DRY RUN SUMMARY ===${NC}"
    echo -e "  Files to deploy:"
    echo "$DEPLOY_FILES" | while read -r f; do echo "    $f"; done
    echo -e "  Service restart: $([ "$NEEDS_RESTART" = true ] && echo -e "${YELLOW}YES${NC}" || echo -e "${GREEN}NO (zero downtime)${NC}")"
    echo -e "  Pip install:     $([ -n "$REQS_CHANGED" ] && echo -e "${YELLOW}YES${NC}" || echo "NO")"
    echo -e "  Cache bumped:    $([ "$CACHE_BUMPED" = true ] && echo "YES" || echo "NO")"
    echo ""
    warn "Dry run — no files were deployed."
    # Revert cache bumps made locally during dry run
    if [ "$CACHE_BUMPED" = true ]; then
        git checkout -- frontend/index.html frontend/admin.html 2>/dev/null || true
        warn "Reverted local cache bump changes"
    fi
    exit 0
fi

# ─── Pre-deploy backup on server ────────────────────────────────────────────
info "Creating backup of current production files on server..."
ssh "${SSH_TARGET}" bash -s <<'BACKUP_EOF'
    REMOTE_DIR="/opt/epstein"
    BACKUP_DIR="${REMOTE_DIR}/.deploy-backup"
    sudo rm -rf "$BACKUP_DIR"
    sudo mkdir -p "${BACKUP_DIR}/backend" "${BACKUP_DIR}/frontend" "${BACKUP_DIR}/scripts"
    sudo cp -f ${REMOTE_DIR}/backend/*.py "${BACKUP_DIR}/backend/" 2>/dev/null || true
    sudo cp -f ${REMOTE_DIR}/frontend/* "${BACKUP_DIR}/frontend/" 2>/dev/null || true
    sudo cp -f ${REMOTE_DIR}/scripts/*.py "${BACKUP_DIR}/scripts/" 2>/dev/null || true
    sudo cp -f "${REMOTE_DIR}/run.py" "${BACKUP_DIR}/" 2>/dev/null || true
    sudo chown -R user:user "$BACKUP_DIR"
BACKUP_EOF
success "Backup created at ${BACKUP_DIR}/"

# ─── Stage files on server ───────────────────────────────────────────────────
info "Preparing staging directories on server..."
ssh "${SSH_TARGET}" "rm -rf /tmp/epstein-deploy && mkdir -p /tmp/epstein-deploy/{backend,frontend,scripts}"

# SCP each category of changed files
if [ -n "$BACKEND_CHANGED" ]; then
    info "Uploading backend files..."
    scp -q backend/*.py "${SSH_TARGET}:/tmp/epstein-deploy/backend/"
    success "Backend files uploaded"
fi

if [ -n "$FRONTEND_CHANGED" ]; then
    info "Uploading frontend files..."
    # Only copy safe frontend file types
    for ext in html js css svg xml png; do
        # Use a subshell so glob failures don't kill the script
        files=$(ls frontend/*.${ext} 2>/dev/null || true)
        if [ -n "$files" ]; then
            scp -q frontend/*.${ext} "${SSH_TARGET}:/tmp/epstein-deploy/frontend/"
        fi
    done
    success "Frontend files uploaded"
fi

if [ -n "$SCRIPTS_CHANGED" ]; then
    info "Uploading script files..."
    scp -q scripts/*.py "${SSH_TARGET}:/tmp/epstein-deploy/scripts/"
    # scripts/ also has scrape.js
    [ -f scripts/scrape.js ] && scp -q scripts/scrape.js "${SSH_TARGET}:/tmp/epstein-deploy/scripts/"
    success "Script files uploaded"
fi

if [ -n "$RUNPY_CHANGED" ]; then
    info "Uploading run.py..."
    scp -q run.py "${SSH_TARGET}:/tmp/epstein-deploy/"
    success "run.py uploaded"
fi

if [ -n "$REQS_CHANGED" ]; then
    info "Uploading requirements.txt..."
    scp -q requirements.txt "${SSH_TARGET}:/tmp/epstein-deploy/"
    success "requirements.txt uploaded"
fi

# ─── Remote install ──────────────────────────────────────────────────────────
info "Installing files on server..."
ssh "${SSH_TARGET}" bash -s -- "$NEEDS_RESTART" "$( [ -n "$REQS_CHANGED" ] && echo true || echo false )" <<'INSTALL_EOF'
    NEEDS_RESTART="$1"
    NEEDS_PIP="$2"
    REMOTE_DIR="/opt/epstein"
    STAGING="/tmp/epstein-deploy"

    # Copy staged files to production
    [ -n "$(ls ${STAGING}/backend/ 2>/dev/null)" ]  && sudo cp -f ${STAGING}/backend/*  ${REMOTE_DIR}/backend/
    [ -n "$(ls ${STAGING}/frontend/ 2>/dev/null)" ] && sudo cp -f ${STAGING}/frontend/* ${REMOTE_DIR}/frontend/
    [ -n "$(ls ${STAGING}/scripts/ 2>/dev/null)" ]  && sudo cp -f ${STAGING}/scripts/*  ${REMOTE_DIR}/scripts/
    [ -f "${STAGING}/run.py" ]                       && sudo cp -f ${STAGING}/run.py     ${REMOTE_DIR}/
    [ -f "${STAGING}/requirements.txt" ]             && sudo cp -f ${STAGING}/requirements.txt ${REMOTE_DIR}/

    # Fix ownership
    sudo chown -R user:user "$REMOTE_DIR"

    # Install deps if requirements.txt changed
    if [ "$NEEDS_PIP" = "true" ]; then
        echo "[INFO] Installing Python dependencies..."
        sudo ${REMOTE_DIR}/venv/bin/pip install -r ${REMOTE_DIR}/requirements.txt --quiet
    fi

    # Restart service if needed
    if [ "$NEEDS_RESTART" = "true" ]; then
        echo "[INFO] Restarting ${REMOTE_DIR} service..."
        sudo systemctl restart epstein
        sleep 3
        if systemctl is-active --quiet epstein; then
            echo "[OK] Service is active"
        else
            echo "[ERR] Service failed to start!"
            sudo journalctl -u epstein --no-pager -n 20
            exit 1
        fi
    else
        echo "[SKIP] Frontend-only deploy — service restart not needed"
    fi

    # Cleanup staging
    rm -rf "$STAGING"
INSTALL_EOF

success "Files installed on server"

# ─── Record deployed commit SHA on server ────────────────────────────────────
if [ "$DRY_RUN" = false ]; then
    ssh "${SSH_TARGET}" "echo '${CURRENT_SHA}' > ${REMOTE_DIR}/.deploy-sha"
    success "Recorded deploy marker: ${CURRENT_SHA:0:7}"
fi

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}=== DEPLOY COMPLETE ===${NC}"
echo -e "  Files deployed:"
echo "$DEPLOY_FILES" | while read -r f; do echo "    $f"; done
echo -e "  Cache bumped:    $([ "$CACHE_BUMPED" = true ] && echo "YES" || echo "NO")"
echo -e "  Pip install:     $([ -n "$REQS_CHANGED" ] && echo "YES" || echo "NO")"
if [ "$NEEDS_RESTART" = true ]; then
    echo -e "  Service restart: ${GREEN}YES${NC}"
    # Final verification
    STATUS=$(ssh "${SSH_TARGET}" "systemctl is-active ${SERVICE_NAME}" 2>/dev/null || echo "unknown")
    echo -e "  Service status:  $([ "$STATUS" = "active" ] && echo -e "${GREEN}${STATUS}${NC}" || echo -e "${RED}${STATUS}${NC}")"
else
    echo -e "  Service restart: ${YELLOW}SKIPPED (frontend-only, zero downtime)${NC}"
fi
echo ""
