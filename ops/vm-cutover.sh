#!/usr/bin/env bash
# Move production from the old VM (epstein-vm, 10.0.0.4) to the new one (epstein-vm2, 10.0.0.5).
#
#   ops/vm-cutover.sh go         stop the app on the old VM, final sync, start it on the
#                                new VM, move the public IP, check the live site
#   ops/vm-cutover.sh rollback   move the public IP back and start the app on the old VM
#
# Run from your Mac in the main checkout (needs `az login` and .deploy.env).
# Downtime during `go` is the final sync plus the IP move: typically 5 to 10 minutes.
# The old VM is left stopped-but-intact (not deleted), so rollback works until it is.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/.deploy.env"
OLD="${SSH_USER:?}@${SSH_HOST:?}"
NEW_IP=10.0.0.5
RG=epstein-rg
PIP=epstein-ip
OLD_NIC=epstein-nic;      OLD_IPC=ipconfig1
NEW_NIC=epstein-vm2VMNic; NEW_IPC=ipconfigepstein-vm2
SITE=https://epsteinfta.com

ssh-add -l >/dev/null 2>&1 || ssh-add ~/.ssh/id_rsa
on_new() { ssh -o BatchMode=yes -J "$OLD" "$SSH_USER@$NEW_IP" "$@"; }

wait_for() {  # wait_for <description> <command...>
    local what=$1; shift
    for _ in $(seq 1 60); do "$@" >/dev/null 2>&1 && { echo "  $what: ok"; return 0; }; sleep 5; done
    echo "  $what: NOT ok after 5 minutes" >&2; return 1
}

move_ip() {  # move_ip <from_nic> <from_ipc> <to_nic> <to_ipc>
    echo "Moving public IP $PIP from $1 to $3..."
    az network nic ip-config update -g "$RG" --nic-name "$1" -n "$2" --remove publicIpAddress -o none
    az network nic ip-config update -g "$RG" --nic-name "$3" -n "$4" --public-ip-address "$PIP" -o none
}

case "${1:-}" in
go)
    echo "1/5 Stopping the app on the old VM (site goes down now)..."
    ssh "$OLD" 'sudo systemctl stop epstein && sudo systemctl disable epstein 2>/dev/null; systemctl is-active epstein || true'

    echo "2/5 Final sync of app files and databases to the new VM..."
    ssh -A "$OLD" 'cd /opt/epstein && rsync -a --delete --stats \
      --exclude="/DOJ Disclosures" --exclude="/House Disclosures" --exclude="/CourtRecords" \
      --exclude="/FOIA" --exclude="/FBI Vault" --exclude="/.r2cache" --exclude="/.env.pre-r2" \
      --exclude="/.predeploy-backup" --exclude="/ops" \
      /opt/epstein/ '"$NEW_IP"':/opt/epstein/ | grep -E "files transferred|total size"'

    echo "3/5 Starting the app on the new VM..."
    on_new 'sudo systemctl enable --now epstein >/dev/null 2>&1; sudo systemctl restart epstein'
    wait_for "new VM answers locally" on_new 'curl -fsS -o /dev/null -H "Host: epsteinfta.com" http://127.0.0.1/api/stats'

    echo "4/5 Moving the public IP..."
    move_ip "$OLD_NIC" "$OLD_IPC" "$NEW_NIC" "$NEW_IPC"

    echo "5/5 Checking the live site..."
    wait_for "$SITE/api/stats" curl -fsS -o /dev/null "$SITE/api/stats"
    ssh -o ConnectTimeout=20 "$OLD" 'hostname; systemctl is-active epstein'
    echo "Done. The live site now runs on epstein-vm2. The old VM is still running with the app stopped;"
    echo "deallocate it once you are happy:  az vm deallocate -g $RG -n epstein-vm"
    ;;
rollback)
    move_ip "$NEW_NIC" "$NEW_IPC" "$OLD_NIC" "$OLD_IPC"
    az vm start -g "$RG" -n epstein-vm -o none
    wait_for "old VM ssh" ssh -o ConnectTimeout=10 "$OLD" true
    ssh "$OLD" 'sudo systemctl enable --now epstein >/dev/null 2>&1; sudo systemctl restart epstein'
    wait_for "$SITE/api/stats" curl -fsS -o /dev/null "$SITE/api/stats"
    echo "Rolled back to epstein-vm. Data written on the new VM since cutover is NOT copied back."
    ;;
*)
    echo "usage: $0 go|rollback" >&2; exit 2 ;;
esac
