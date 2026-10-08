#!/usr/bin/env bash
# Make the nginx proxy cache key include the query string.
#
# Symptom: /api/subcategories?category=FOIA was answered from the cache entry stored for
# ?category=DOJ Disclosures (x-cache-status: HIT), because a custom proxy_cache_key used
# $uri (path only) instead of $request_uri (path + query).
#
# Usage (on the server):
#   sudo bash fix-nginx-cache-key.sh            # dry run: show what would change
#   sudo bash fix-nginx-cache-key.sh --apply    # back up, edit, nginx -t, reload (rolls back on failure)
set -euo pipefail

APPLY=false
[ "${1:-}" = "--apply" ] && APPLY=true
if $APPLY && [ "$(id -u)" -ne 0 ] && [ -z "${NGINX_FIX_TEST:-}" ]; then echo "Run with sudo." >&2; exit 1; fi

NGINX_DIR=${NGINX_DIR:-/etc/nginx}
# Backups live OUTSIDE the nginx tree: a copy left in sites-enabled/ would be loaded as config.
BACKUP_DIR=${BACKUP_DIR:-/root/nginx-backups}
FILES=()
while IFS= read -r f; do FILES+=("$f"); done < <(grep -rlE '^[[:space:]]*proxy_cache_key\b' "$NGINX_DIR" 2>/dev/null | grep -vE '\.(bak|orig|dpkg-[a-z]+)|~$' || true)

if [ "${#FILES[@]}" -eq 0 ]; then
    echo "No proxy_cache_key directive found under $NGINX_DIR."
    echo "nginx's default key (\$scheme\$proxy_host\$request_uri) already includes the query string,"
    echo "so the cache key is not the cause. proxy_cache usage for reference:"
    grep -rnE '^\s*proxy_cache(_valid|_path|_key|_bypass|_ignore_headers)?\b|proxy_ignore_headers' "$NGINX_DIR" || true
    exit 2
fi

needs_fix() {  # a key line that has neither $request_uri nor $args/$query_string
    grep -nE '^[[:space:]]*proxy_cache_key\b' "$1" | grep -vE '\$request_uri|\$args|\$query_string|\$is_args' || true
}

CHANGED=()
for f in "${FILES[@]}"; do
    echo "== $f"
    grep -nE '^[[:space:]]*proxy_cache_key\b' "$f"
    if [ -n "$(needs_fix "$f")" ]; then
        if grep -nE '^[[:space:]]*proxy_cache_key\b' "$f" | grep -vE '\$request_uri|\$args|\$query_string|\$is_args' | grep -q '\$uri'; then
            echo "   -> will replace \$uri with \$request_uri on the line(s) above"
            CHANGED+=("$f")
        else
            echo "   -> key has no \$uri to swap; edit by hand to append \$is_args\$args"
        fi
    else
        echo "   -> already includes the query string"
    fi
done

# Also report anything that would stop the app's X-Accel-Expires opt-out from working.
if grep -rnE 'proxy_ignore_headers[^;]*X-Accel-Expires' "$NGINX_DIR" 2>/dev/null; then
    echo "NOTE: proxy_ignore_headers lists X-Accel-Expires (the app's per-response cache opt-out is ignored)."
fi

if [ "${#CHANGED[@]}" -eq 0 ]; then
    echo "Nothing to change automatically."
    exit 0
fi
if ! $APPLY; then
    echo
    echo "Dry run only. Re-run with --apply to make the change."
    exit 0
fi

STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR/$STAMP"
backup_of() { echo "$BACKUP_DIR/$STAMP/$(echo "$1" | tr / _)"; }
for f in "${CHANGED[@]}"; do
    cp -p "$f" "$(backup_of "$f")"
    # Only on proxy_cache_key lines without a query component: $uri -> $request_uri.
    perl -i -pe 's/\$uri\b/\$request_uri/g if /^\s*proxy_cache_key\b/ && !/\$(?:request_uri|args|query_string|is_args)/' "$f"
    echo "Edited $f (backup: $(backup_of "$f"))"
    grep -nE '^[[:space:]]*proxy_cache_key\b' "$f"
done

if nginx -t; then
    systemctl reload nginx
    echo "nginx reloaded. Old cache entries are simply never looked up again under the new key."
else
    echo "nginx -t failed; restoring backups." >&2
    for f in "${CHANGED[@]}"; do cp -p "$(backup_of "$f")" "$f"; done
    nginx -t && echo "Backups restored; nginx config unchanged." >&2
    exit 1
fi
