#!/usr/bin/env bash
# Copy the document folders from the VM's disk to the R2 bucket. Runs ON the VM as root.
#
#   ops/r2-sync.sh install   install rclone (official .deb, pinned version) if missing
#   ops/r2-sync.sh start     start the copy as a transient systemd unit (survives logout)
#   ops/r2-sync.sh status    unit state plus the tail of the log
#   ops/r2-sync.sh check     compare local files with R2 by size (read-only)
#
# Credentials come from R2_* in /opt/epstein/.env (see ops/r2-set-credentials.sh).
# Re-running `start` is safe: rclone skips objects that already match by size.
set -euo pipefail

BASE="${EPSTEIN_BASE_PATH:-/opt/epstein}"
LOG="$BASE/logs/r2-sync.log"
DIRS=("DOJ Disclosures" "House Disclosures" "CourtRecords" "FOIA" "FBI Vault")
RCLONE_VERSION="v1.68.2"

load_env() {
    set -a
    # shellcheck disable=SC1091
    source <(grep -E '^R2_[A-Z_]+=' "$BASE/.env")
    set +a
    : "${R2_ACCOUNT_ID:?R2_ACCOUNT_ID missing from .env}"
    : "${R2_ACCESS_KEY_ID:?R2_ACCESS_KEY_ID missing from .env}"
    : "${R2_SECRET_ACCESS_KEY:?R2_SECRET_ACCESS_KEY missing from .env}"
    export RCLONE_CONFIG_R2_TYPE=s3
    export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
    export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
    export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
    export RCLONE_CONFIG_R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    export RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
    BUCKET="${R2_BUCKET:-epsteinfta-docs}"
}

case "${1:-}" in
install)
    if command -v rclone >/dev/null && rclone version | head -1 | grep -q "v1\.\(6[0-9]\|[7-9][0-9]\)"; then
        rclone version | head -1; exit 0
    fi
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/rclone.deb" \
        "https://downloads.rclone.org/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-amd64.deb"
    dpkg -i "$tmp/rclone.deb"
    rm -rf "$tmp"
    rclone version | head -1
    ;;
run)
    # The body of the systemd unit. Copies each folder to the same path in the bucket.
    load_env
    mkdir -p "$(dirname "$LOG")"
    for d in "${DIRS[@]}"; do
        [ -d "$BASE/$d" ] || continue
        echo "=== $(date -u +%FT%TZ) copying: $d"
        rclone copy "$BASE/$d" "r2:$BUCKET/$d" \
            --size-only --transfers 48 --checkers 64 --fast-list \
            --exclude "*.bak" --exclude "*.part" --exclude ".*" \
            --s3-upload-concurrency 4 --retries 5 --low-level-retries 20 \
            --stats 60s --stats-one-line --stats-log-level NOTICE
    done
    echo "=== $(date -u +%FT%TZ) done"
    ;;
start)
    load_env >/dev/null
    if systemctl is-active --quiet r2-sync; then echo "r2-sync is already running"; exit 0; fi
    mkdir -p "$(dirname "$LOG")"
    systemctl reset-failed r2-sync 2>/dev/null || true
    systemd-run --unit=r2-sync --description="Copy documents to R2" \
        --property=Nice=10 --property=IOSchedulingClass=idle \
        --property=StandardOutput="append:$LOG" --property=StandardError="append:$LOG" \
        /bin/bash "$(readlink -f "$0")" run
    echo "started; log: $LOG"
    ;;
status)
    systemctl show r2-sync -p ActiveState -p SubState -p ExecMainStatus 2>/dev/null || true
    tail -n 15 "$LOG" 2>/dev/null || echo "no log yet"
    ;;
check)
    load_env
    for d in "${DIRS[@]}"; do
        [ -d "$BASE/$d" ] || continue
        echo "=== $d"
        rclone check "$BASE/$d" "r2:$BUCKET/$d" --one-way --size-only --fast-list \
            --exclude "*.bak" --exclude "*.part" --exclude ".*" --checkers 64 2>&1 | tail -4
    done
    ;;
*)
    echo "usage: $0 install|start|status|check" >&2; exit 2 ;;
esac
