#!/usr/bin/env bash
# Turn serving of document files from R2 on or off on the production VM.
#
#   ops/r2-serve.sh on     set R2_SERVE=1 and restart the service (files redirect to R2)
#   ops/r2-serve.sh off    set R2_SERVE=0 and restart (files served from the VM disk again)
#
# Run from your Mac in the main checkout. The restart gives about 2 minutes of 502s.
# Run ops/r2-start-copy.sh check first: only files present in R2 are redirected,
# anything missing still falls back to the local disk.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/.deploy.env"
SSH_TARGET="${SSH_USER:?}@${SSH_HOST:?}"
REMOTE_ENV="${REMOTE_DIR:-/opt/epstein}/.env"
SERVICE="${SERVICE_NAME:-epstein}"

case "${1:-}" in
on) VALUE=1 ;;
off) VALUE=0 ;;
*) echo "usage: $0 on|off" >&2; exit 2 ;;
esac

ssh "$SSH_TARGET" "sudo python3 -c '
import sys, os
path, value = sys.argv[1], sys.argv[2]
st = os.stat(path)
lines = [l for l in open(path).read().splitlines() if not l.startswith(\"R2_SERVE=\")]
lines.append(\"R2_SERVE=\" + value)
with open(path, \"w\") as f:
    f.write(\"\n\".join(lines) + \"\n\")
os.chown(path, st.st_uid, st.st_gid)
os.chmod(path, st.st_mode)
print(\"R2_SERVE=\" + value, \"written to\", path)
' $REMOTE_ENV $VALUE && sudo systemctl restart $SERVICE && echo restarted $SERVICE"

echo "Waiting for the site to come back..."
for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "https://epsteinfta.com/api/stats"; then
        echo "Site is up."; exit 0
    fi
    sleep 5
done
echo "Site not answering after 5 minutes; check: ssh $SSH_TARGET 'journalctl -u $SERVICE -n 50'" >&2
exit 1
