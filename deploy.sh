#!/usr/bin/env bash
# deploy.sh — Deploy code files to Azure VM (never deploys database/data files)
set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
# Load optional local overrides from .deploy.env (gitignored).
# Copy .deploy.env.example -> .deploy.env and edit before first run.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
if [ -f "${SCRIPT_DIR}/.deploy.env" ]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/.deploy.env"
fi

SSH_HOST="${SSH_HOST:?Set SSH_HOST in .deploy.env (copy from .deploy.env.example)}"
SSH_USER="${SSH_USER:?Set SSH_USER in .deploy.env (copy from .deploy.env.example)}"
REMOTE_DIR="${REMOTE_DIR:-/opt/epstein}"
SERVICE_NAME="${SERVICE_NAME:-epstein}"
SSH_TARGET="${SSH_USER}@${SSH_HOST}"
BACKUP_DIR="${REMOTE_DIR}/.deploy-backup"
VENV_PIP="${REMOTE_DIR}/venv/bin/pip"

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
    ssh "${SSH_TARGET}" bash -s -- "$REMOTE_DIR" "$SSH_USER" "$SERVICE_NAME" <<'ROLLBACK_EOF'
        REMOTE_DIR="$1"
        SSH_USER="$2"
        SERVICE_NAME="$3"
        BACKUP_DIR="${REMOTE_DIR}/.deploy-backup"
        if [ ! -d "$BACKUP_DIR" ]; then
            echo "ERROR: No backup found at $BACKUP_DIR"
            exit 1
        fi
        # Restore each backed-up directory
        for dir in backend frontend; do
            if [ -d "${BACKUP_DIR}/${dir}" ]; then
                sudo cp -f "${BACKUP_DIR}/${dir}/"* "${REMOTE_DIR}/${dir}/"
            fi
        done
        [ -f "${BACKUP_DIR}/run.py" ] && sudo cp -f "${BACKUP_DIR}/run.py" "${REMOTE_DIR}/"
        # Scope to restored targets only — see note in the main install block.
        sudo chown "${SSH_USER}:${SSH_USER}" \
            ${REMOTE_DIR}/backend/*.py ${REMOTE_DIR}/frontend/* \
            ${REMOTE_DIR}/run.py ${REMOTE_DIR}/requirements.txt 2>/dev/null || true
        sudo systemctl restart "$SERVICE_NAME"
        sleep 3
        systemctl is-active "$SERVICE_NAME"
ROLLBACK_EOF
    success "Rollback complete and service restarted"
    exit 0
fi

# ─── Detect changed files ───────────────────────────────────────────────────
info "Detecting changed files..."

CURRENT_SHA=$(git rev-parse HEAD)

if [ "$DEPLOY_ALL" = true ]; then
    info "Deploying ALL code files (--all flag)"
    CHANGED_FILES=$(git ls-files -- backend/ frontend/ run.py requirements.txt)
else
    # Fetch the last deployed commit SHA from the server
    LAST_DEPLOYED_SHA=$(ssh "${SSH_TARGET}" "cat ${REMOTE_DIR}/.deploy-sha 2>/dev/null" || true)

    if [ -z "$LAST_DEPLOYED_SHA" ]; then
        info "No previous deploy marker found on server — deploying all code files"
        CHANGED_FILES=$(git ls-files -- backend/ frontend/ run.py requirements.txt)
    elif [ "$LAST_DEPLOYED_SHA" = "$CURRENT_SHA" ]; then
        # Same commit — check for uncommitted local changes
        CHANGED_FILES=$(git diff --name-only HEAD 2>/dev/null || true)
        UNTRACKED=$(git ls-files --others --exclude-standard -- backend/ frontend/ run.py requirements.txt 2>/dev/null || true)
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
        CHANGED_FILES=$(git diff --name-only "$LAST_DEPLOYED_SHA" HEAD 2>/dev/null || git ls-files -- backend/ frontend/ run.py requirements.txt)
        # Also include uncommitted changes on top of HEAD
        UNCOMMITTED=$(git diff --name-only HEAD 2>/dev/null || true)
        UNTRACKED=$(git ls-files --others --exclude-standard -- backend/ frontend/ run.py requirements.txt 2>/dev/null || true)
        if [ -n "$UNCOMMITTED" ] || [ -n "$UNTRACKED" ]; then
            CHANGED_FILES=$(printf "%s\n%s\n%s" "$CHANGED_FILES" "$UNCOMMITTED" "$UNTRACKED" | sort -u)
        fi
    fi
fi

# Filter to only files in deployable directories
DEPLOY_FILES=$(echo "$CHANGED_FILES" | grep -E '^(backend/|frontend/|run\.py$|requirements\.txt$)' || true)

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
# Bump ?v=N query strings so browsers re-fetch changed JS/CSS. On a real run the
# edits are committed further below so they don't linger as uncommitted diffs
# (which would make every later run re-detect them and re-bump forever). On a
# dry run nothing is written, so no revert dance is needed.
CACHE_BUMPED=false
BUMPED_FILES=""

bump_version() {  # $1 = asset filename (e.g. app.js)   $2 = html file to edit
    local asset="$1" file="$2" cur new
    echo "$JS_CSS_CHANGED" | grep -q "$asset" || return 0
    cur=$(sed -n "s/.*${asset}?v=\([0-9]*\).*/\1/p" "$file" | head -1)
    cur=${cur:-0}
    new=$((cur + 1))
    if [ "$DRY_RUN" = false ]; then
        sed -i.bak "s/${asset}?v=${cur}/${asset}?v=${new}/" "$file" && rm -f "${file}.bak"
        case " $BUMPED_FILES " in *" $file "*) ;; *) BUMPED_FILES="$BUMPED_FILES $file" ;; esac
        success "Bumped ${asset} v=${cur} -> v=${new} in $(basename "$file")"
    else
        info "Would bump ${asset} v=${cur} -> v=${new} in $(basename "$file")"
    fi
    CACHE_BUMPED=true
}

if [ -n "$FRONTEND_CHANGED" ]; then
    JS_CSS_CHANGED=$(echo "$FRONTEND_CHANGED" | grep -E '\.(js|css)$' || true)
    if [ -n "$JS_CSS_CHANGED" ]; then
        info "Frontend JS/CSS changed — bumping cache versions..."
        bump_version 'styles.css' frontend/index.html
        bump_version 'app.js'     frontend/index.html
        bump_version 'admin.js'   frontend/admin.html
    else
        warn "Frontend changed but no JS/CSS files — skipping cache bust"
    fi
fi

# ─── Dry run stops here ─────────────────────────────────────────────────────
if [ "$DRY_RUN" = true ]; then
    echo ""
    echo -e "${BOLD}=== DRY RUN SUMMARY ===${NC}"
    echo -e "  Files to deploy:"
    echo "$DEPLOY_FILES" | while read -r f; do echo "    $f"; done
    echo -e "  Service restart: $([ "$NEEDS_RESTART" = true ] && echo -e "${YELLOW}YES${NC}" || echo -e "${GREEN}NO (zero downtime)${NC}")"
    echo -e "  Pip install:     $([ -n "$REQS_CHANGED" ] && echo -e "${YELLOW}YES${NC}" || echo "NO")"
    echo -e "  Cache bump:      $([ "$CACHE_BUMPED" = true ] && echo "YES (would bump)" || echo "NO")"
    echo ""
    warn "Dry run — no files were deployed or modified."
    exit 0
fi

# ─── Persist cache bumps so re-runs are idempotent ───────────────────────────
# The ?v= bumps mutate index.html/admin.html. Committing them here means a
# re-run with no further source changes diffs clean against the recorded SHA
# and exits "nothing to do" — instead of re-detecting the bump and looping.
if [ "$CACHE_BUMPED" = true ] && [ -n "$BUMPED_FILES" ]; then
    info "Committing cache-version bumps so re-runs stay idempotent..."
    git add -- $BUMPED_FILES 2>/dev/null || true
    if git commit -q -m "deploy: bump frontend cache versions" -- $BUMPED_FILES 2>/dev/null; then
        CURRENT_SHA=$(git rev-parse HEAD)
        success "Committed cache bumps (${CURRENT_SHA:0:7})"
    else
        warn "Nothing to commit for cache bumps (continuing)"
    fi
fi

# ─── Pre-deploy backup on server ────────────────────────────────────────────
info "Creating backup of current production files on server..."
ssh "${SSH_TARGET}" bash -s -- "$REMOTE_DIR" "$SSH_USER" <<'BACKUP_EOF'
    REMOTE_DIR="$1"
    SSH_USER="$2"
    BACKUP_DIR="${REMOTE_DIR}/.deploy-backup"
    sudo rm -rf "$BACKUP_DIR"
    sudo mkdir -p "${BACKUP_DIR}/backend" "${BACKUP_DIR}/frontend"
    sudo cp -f ${REMOTE_DIR}/backend/*.py "${BACKUP_DIR}/backend/" 2>/dev/null || true
    sudo cp -f ${REMOTE_DIR}/frontend/* "${BACKUP_DIR}/frontend/" 2>/dev/null || true
    sudo cp -f "${REMOTE_DIR}/run.py" "${BACKUP_DIR}/" 2>/dev/null || true
    sudo chown -R "${SSH_USER}:${SSH_USER}" "$BACKUP_DIR"
BACKUP_EOF
success "Backup created at ${BACKUP_DIR}/"

# ─── Upload exactly the changed files (file-scoped, paths preserved) ─────────
# Only the files in DEPLOY_FILES are transferred — not whole categories — so a
# one-file change ships one file and unrelated working-tree edits never ride
# along. tar preserves each file's repo-relative path (backend/x.py,
# frontend/y.html, run.py, ...) and uses a single connection.
EXISTING_FILES=$(echo "$DEPLOY_FILES" | while read -r f; do [ -n "$f" ] && [ -f "$f" ] && echo "$f"; done)
if [ -z "$EXISTING_FILES" ]; then
    warn "Changed paths are all deletions — nothing to upload. Done."
    exit 0
fi
N_FILES=$(echo "$EXISTING_FILES" | grep -c .)

info "Uploading ${N_FILES} changed file(s) to server..."
ssh "${SSH_TARGET}" "rm -rf /tmp/epstein-deploy && mkdir -p /tmp/epstein-deploy"

LIST_FILE=$(mktemp)
echo "$EXISTING_FILES" > "$LIST_FILE"
# COPYFILE_DISABLE stops macOS tar from emitting ._ AppleDouble sidecar files.
COPYFILE_DISABLE=1 tar -czf - -T "$LIST_FILE" | ssh "${SSH_TARGET}" "tar -xzf - -C /tmp/epstein-deploy"
rm -f "$LIST_FILE"
success "Uploaded ${N_FILES} file(s)"

# ─── Remote install ──────────────────────────────────────────────────────────
info "Installing files on server..."
ssh "${SSH_TARGET}" bash -s -- \
    "$NEEDS_RESTART" \
    "$( [ -n "$REQS_CHANGED" ] && echo true || echo false )" \
    "$REMOTE_DIR" \
    "$SSH_USER" \
    "$SERVICE_NAME" <<'INSTALL_EOF'
    NEEDS_RESTART="$1"
    NEEDS_PIP="$2"
    REMOTE_DIR="$3"
    SSH_USER="$4"
    SERVICE_NAME="$5"
    STAGING="/tmp/epstein-deploy"

    # Copy exactly the staged (changed) files to production, preserving paths,
    # and chown only those files.
    # NEVER chown -R the whole REMOTE_DIR: it holds multi-GB DBs and ~2.8M
    # files under extracted_text/ + thumbnails/, so a recursive walk takes
    # minutes and evicts epstein.db from the page cache on every deploy.
    cd "$STAGING" 2>/dev/null || { echo "[ERR] staging dir missing"; exit 1; }
    find . -type f | sed 's|^\./||' | while read -r rel; do
        sudo mkdir -p "${REMOTE_DIR}/$(dirname "$rel")"
        sudo cp -f "${STAGING}/${rel}" "${REMOTE_DIR}/${rel}"
        sudo chown "${SSH_USER}:${SSH_USER}" "${REMOTE_DIR}/${rel}"
    done
    cd / 2>/dev/null || true

    # Install deps if requirements.txt changed
    if [ "$NEEDS_PIP" = "true" ]; then
        echo "[INFO] Installing Python dependencies..."
        sudo ${REMOTE_DIR}/venv/bin/pip install -r ${REMOTE_DIR}/requirements.txt --quiet
    fi

    # Restart service if needed
    if [ "$NEEDS_RESTART" = "true" ]; then
        echo "[INFO] Restarting ${SERVICE_NAME} service..."
        sudo systemctl restart "$SERVICE_NAME"
        sleep 3
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            echo "[OK] Service is active"
        else
            echo "[ERR] Service failed to start!"
            sudo journalctl -u "$SERVICE_NAME" --no-pager -n 20
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
