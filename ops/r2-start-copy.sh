#!/usr/bin/env bash
# Start copying the document folders from the production VM to R2.
#
# Run from your Mac in the main checkout, after ops/r2-set-credentials.sh.
# Uploads ops/r2-sync.sh to the VM, installs rclone (official .deb, pinned) if
# needed, and starts the copy as a systemd unit that keeps running after you
# disconnect. Safe to re-run: it resumes and skips files already in R2.
#
# Check progress any time with:  ops/r2-start-copy.sh status
# Verify when finished with:     ops/r2-start-copy.sh check
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/.deploy.env"
SSH_TARGET="${SSH_USER:?}@${SSH_HOST:?}"
REMOTE="${REMOTE_DIR:-/opt/epstein}"

case "${1:-start}" in
start)
    ssh "$SSH_TARGET" "sudo mkdir -p '$REMOTE/ops'"
    ssh "$SSH_TARGET" "sudo tee '$REMOTE/ops/r2-sync.sh' >/dev/null && sudo chmod 755 '$REMOTE/ops/r2-sync.sh'" \
        < "$SCRIPT_DIR/ops/r2-sync.sh"
    ssh "$SSH_TARGET" "sudo '$REMOTE/ops/r2-sync.sh' install && sudo '$REMOTE/ops/r2-sync.sh' start"
    ;;
status|check)
    ssh "$SSH_TARGET" "sudo '$REMOTE/ops/r2-sync.sh' $1"
    ;;
*)
    echo "usage: $0 [start|status|check]" >&2; exit 2 ;;
esac
