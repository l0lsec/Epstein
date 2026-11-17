#!/usr/bin/env bash
# Store the R2 API credentials in /opt/epstein/.env on the production VM.
#
# Run this from your Mac in the main checkout (it reads SSH details from .deploy.env).
# It prompts for the Access Key ID and Secret Access Key of an R2 API token
# (Cloudflare dashboard > R2 > Manage API tokens > Create Account API token,
# permission "Object Read & Write", scoped to the epsteinfta-docs bucket).
# The secret is typed into a hidden prompt and sent over SSH stdin: it never
# appears in your shell history, the process list or this repo.
#
# R2_SERVE is written as 0 (files still served from the VM). Flip it to 1 once
# the copy to R2 is verified. The service is NOT restarted here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/.deploy.env"
SSH_TARGET="${SSH_USER:?}@${SSH_HOST:?}"
REMOTE_ENV="${REMOTE_DIR:-/opt/epstein}/.env"

ACCOUNT_ID="${R2_ACCOUNT_ID:-834b30cbb3f5f3ed6e289322814882b5}"
BUCKET="${R2_BUCKET:-epsteinfta-docs}"

read -r -p "R2 Access Key ID: " KEY_ID
read -r -s -p "R2 Secret Access Key (hidden): " SECRET
echo
[ -n "$KEY_ID" ] && [ -n "$SECRET" ] || { echo "Both values are required." >&2; exit 1; }

# Replace any existing R2_* lines, keep everything else, keep the file's owner/mode.
printf 'R2_ACCOUNT_ID=%s\nR2_ACCESS_KEY_ID=%s\nR2_SECRET_ACCESS_KEY=%s\nR2_BUCKET=%s\nR2_SERVE=0\n' \
    "$ACCOUNT_ID" "$KEY_ID" "$SECRET" "$BUCKET" |
ssh "$SSH_TARGET" "sudo python3 -c '
import sys, os, shutil
path = sys.argv[1]
new = sys.stdin.read().splitlines()
keys = {l.split(\"=\", 1)[0] for l in new}
old = open(path).read().splitlines() if os.path.exists(path) else []
kept = [l for l in old if l.split(\"=\", 1)[0] not in keys]
shutil.copy2(path, path + \".pre-r2\")
st = os.stat(path)
with open(path, \"w\") as f:
    f.write(\"\n\".join(kept + new) + \"\n\")
os.chown(path, st.st_uid, st.st_gid)
os.chmod(path, st.st_mode)
print(\"Wrote\", len(new), \"R2 settings to\", path, \"(backup:\", path + \".pre-r2)\")
' $REMOTE_ENV"
unset SECRET
