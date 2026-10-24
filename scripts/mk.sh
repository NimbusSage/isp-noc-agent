#!/usr/bin/env bash
# Run a RouterOS command on a MikroTik using stored credentials (tried in order).
# usage: mk.sh <router_ip> '<routeros command>'
#
# Credentials: NOC_MIKROTIK_SSH_PASSWORD (optional NOC_MIKROTIK_SSH_USER, must be admin here)
# or the encrypted MCP store (credentials.enc + NOC_MCP_PASSPHRASE in the environment or
# .env) via scripts/credstore.py. The plaintext mcp-server/config/credentials.json is NOT read.
# Passwords reach ssh through sshpass -e (SSHPASS env), never argv. Host keys: see ssh_opts.sh
# (SSH_HOSTKEY_CHECKING, default accept-new).
set -uo pipefail
if [[ $# -ne 2 ]]; then echo "usage: mk.sh <router_ip> '<routeros command>'" >&2; exit 2; fi
ROUTER="$1"; CMD="$2"
# Harness-proofing: resolve helpers against this script's location, not the caller cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=ssh_opts.sh
source "$SCRIPT_DIR/ssh_opts.sh"
noc_valid_ipv4 "$ROUTER" || { echo "mk.sh: router must be an IPv4 address, got '$ROUTER'" >&2; exit 2; }
noc_ssh_opts || exit 2

# Passwords come back base64-encoded, one per line (safe in a bash variable, never in argv
# or a temp file). credstore's own error text goes straight to our stderr.
PWS=()
if ! enc="$(python3 "$SCRIPT_DIR/credstore.py" passwords mikrotik-ssh --user admin --b64)"; then
  echo "mk.sh: no usable mikrotik-ssh admin credentials - provide NOC_MIKROTIK_SSH_PASSWORD or the encrypted MCP store" >&2
  exit 1
fi
while IFS= read -r line; do
  [[ -n "$line" ]] && PWS+=("$(printf '%s' "$line" | base64 -d)")
done < <(printf '%s\n' "$enc")

# RouterOS drops bursts of concurrent sessions; retry a few times per credential.
attempt() {
  SSHPASS="$1" sshpass -e ssh "${NOC_SSH_OPTS[@]}" -n "admin@$ROUTER" "$CMD" 2>/dev/null
}
for attempt_no in 1 2 3; do
  for PW in "${PWS[@]}"; do
    [ -z "$PW" ] && continue
    if out=$(attempt "$PW"); then
      printf '%s\n' "$out"
      exit 0
    fi
  done
  sleep $((attempt_no * 8))
done
echo "AUTH_FAILED: no stored credential authenticated on $ROUTER" >&2
exit 1
