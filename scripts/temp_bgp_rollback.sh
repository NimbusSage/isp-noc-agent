#!/usr/bin/env bash
# Temp BGP Rollback — disable only TEMP peers created during outage
# Usage: ./scripts/temp_bgp_rollback.sh [--network <fiber>] [--dry-run]
# Disables (not removes) TEMP prefixed peers on RR(s) and towers.
#
# Org inputs (RR IPs, RR passwords, peer prefixes, oxidized URL) come from the
# UNTRACKED site-pack/topology/temp-bgp-config.json — schema guide in
# site-pack/example/temp-bgp-config.example.json. Fails closed on placeholders.
# An rr_credentials entry may name an environment variable instead of holding the
# password: {"ip": "...", "password_env": "TEMP_BGP_RR1_PASSWORD"}.
#
# Tower credentials: NOC_MIKROTIK_SSH_PASSWORD (+ NOC_MIKROTIK_SSH_USER, default admin)
# or the encrypted MCP store (credentials.enc + NOC_MCP_PASSPHRASE) via
# scripts/credstore.py. The plaintext mcp-server/config/credentials.json is NOT read.
# Passwords reach ssh through sshpass -e (SSHPASS env), never argv. Host keys:
# SSH_HOSTKEY_CHECKING (default accept-new), see scripts/ssh_opts.sh.
set -euo pipefail

NETWORK=""
DRYRUN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRYRUN="1" ;;
    --network) shift; NETWORK="${1:-}" ;;
    --network=*) NETWORK="${1#--network=}" ;;
    -*) ;;
    *) NETWORK="$1" ;;   # bare network key (legacy positional form)
  esac
  shift || true
done
if [[ "$NETWORK" == "--dry-run" ]]; then NETWORK=""; DRYRUN="1"; fi
# Strict network key: it is only ever used as a config lookup key, but keep it boring.
if [[ -n "$NETWORK" && ! "$NETWORK" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$ ]]; then
  echo "temp_bgp_rollback: --network '$NETWORK' must match [A-Za-z0-9][A-Za-z0-9_-]{0,39}" >&2
  exit 2
fi

# Harness-proofing: resolve against this script's location, not the caller cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="$SCRIPT_DIR/../site-pack/topology/temp-bgp-config.json"
# shellcheck source=ssh_opts.sh
source "$SCRIPT_DIR/ssh_opts.sh"
noc_ssh_opts || exit 2

# ---- config load (fail closed: missing file or placeholder values) ----------
# python validates and prints tab-separated "KIND<TAB>VALUE" records; bash reads them as
# DATA (never eval'd): RR_SRC, PREFIX, OXIDIZED, RR <ip>, PW <base64 password>.
if ! CFGOUT="$(python3 - "$CFG" "$NETWORK" <<'PYEOF'
import base64, ipaddress, json, os, re, sys
cfg_path, network = sys.argv[1], sys.argv[2]
try:
    cfg = json.load(open(cfg_path))
except FileNotFoundError:
    sys.exit(f"temp_bgp_rollback: missing {cfg_path} — cp "
             f"site-pack/example/temp-bgp-config.example.json "
             f"site-pack/topology/temp-bgp-config.json and fill in your org's values")
blob = json.dumps(cfg)
if "<rr-password-1>" in blob or "example.invalid" in blob:
    sys.exit("temp_bgp_rollback: config still carries the tracked example's "
             "placeholders — refusing to run against real routers")
network = network or cfg.get("default_network", "")
if not network:
    sys.exit("temp_bgp_rollback: no network given — pass --network <fiber> "
             "or set `default_network` in the temp-bgp config")
def ip4(v, what):
    try:
        return str(ipaddress.IPv4Address(str(v).strip()))
    except ValueError:
        sys.exit(f"temp_bgp_rollback: {what} {v!r} is not a valid IPv4 address")
nets = cfg.get("networks", {})
net = nets.get(network, {})
rrs = [ip4(x, "rr ip") for x in (net.get("rr_ips") or cfg.get("default_rrs") or [])]
rr_src = ip4((net.get("rr_ips") or [cfg.get("test_rr", cfg.get("default_rr", ""))])[0], "RR source")
prefix = cfg.get("peer_prefixes", {}).get(network, "")
if prefix and not re.fullmatch(r"[0-9.]+", prefix):
    sys.exit(f"temp_bgp_rollback: peer prefix {prefix!r} must be digits and dots")
oxidized = (cfg.get("oxidized_base_url") or "").rstrip("/")
if oxidized and not re.match(r"^https?://[^/\s]+$", oxidized):
    sys.exit("temp_bgp_rollback: oxidized_base_url must be an http(s) URL without a path")
pws = []
for c in cfg.get("rr_credentials", []):
    pw = c.get("password") or os.environ.get(c.get("password_env") or "", "")
    if pw:
        pws.append(pw)
print(f"RR_SRC\t{rr_src}")
print(f"PREFIX\t{prefix}")
print(f"OXIDIZED\t{oxidized}")
for r in rrs:
    print(f"RR\t{r}")
for pw in pws:
    print("PW\t" + base64.b64encode(pw.encode()).decode())
PYEOF
)"; then
  # python exited nonzero with its message on stderr — nothing parsed
  echo "temp_bgp_rollback: config load failed (see message above)" >&2
  exit 2
fi
RRS=(); PW_LIST=(); RR_SRC=""; PREFIX=""; OXIDIZED=""
while IFS=$'\t' read -r kind value; do
  case "$kind" in
    RR_SRC) RR_SRC="$value" ;;
    PREFIX) PREFIX="$value" ;;
    OXIDIZED) OXIDIZED="$value" ;;
    RR) RRS+=("$value") ;;
    PW) PW_LIST+=("$(printf '%s' "$value" | base64 -d)") ;;
  esac
done < <(printf '%s\n' "$CFGOUT")
if [[ ${#PW_LIST[@]} -eq 0 ]]; then
  echo "temp_bgp_rollback: no rr_credentials passwords in $CFG (set password or password_env) — cannot authenticate to RRs" >&2
  exit 2
fi

# Tower login: first (highest-priority) mikrotik-ssh credential. credstore's own error
# text goes straight to stderr.
if ! TOWER_ENC="$(python3 "$SCRIPT_DIR/credstore.py" passwords mikrotik-ssh --b64)"; then
  echo "temp_bgp_rollback: no mikrotik-ssh credentials - provide NOC_MIKROTIK_SSH_PASSWORD or the encrypted MCP store" >&2
  exit 2
fi
PW_TOWER="$(printf '%s\n' "$TOWER_ENC" | head -n1 | base64 -d)"
TOWER_USER="${NOC_MIKROTIK_SSH_USER:-admin}"
if [[ ! "$TOWER_USER" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then echo "temp_bgp_rollback: unsafe NOC_MIKROTIK_SSH_USER" >&2; exit 2; fi

# rr_ssh HOST CMD — try each configured RR password; fail fast on success,
# return 1 only when every password hits an authentication failure.
rr_ssh() {
  local host=$1 cmd=$2 pw rc out
  for pw in "${PW_LIST[@]}"; do
    out=$(SSHPASS="$pw" sshpass -e ssh "${NOC_SSH_OPTS[@]}" -o ConnectTimeout=5 admin@"$host" "$cmd" 2>&1) && { printf '%s\n' "$out"; return 0; }
    rc=$?
    if [[ $rc -ne 5 && $rc -ne 255 ]]; then printf '%s\n' "$out"; return 0; fi  # 5/255: sshpass/ssh auth failure
  done
  echo "temp_bgp_rollback: all configured RR passwords failed on $host" >&2
  return 1
}

# tower_ssh HOST CMD — one tower credential, password via SSHPASS env.
tower_ssh() {
  SSHPASS="$PW_TOWER" sshpass -e ssh "${NOC_SSH_OPTS[@]}" -o ConnectTimeout=5 "$TOWER_USER"@"$1" "$2" 2>&1
}

# Collect TEMP peers from RRs and disable
for RR in "${RRS[@]}"; do
  echo "=== RR $RR TEMP peers ==="
  out=$(rr_ssh "$RR" '/routing/bgp/connection print where name~"TEMP "') || { echo "[warn] could not read TEMP peers on $RR"; continue; }
  printf '%s\n' "$out" | head -n 100
  if [[ -z "$DRYRUN" ]]; then
    echo "Disabling TEMP on $RR"
    if rr_ssh "$RR" '/routing/bgp/connection disable [find where name~"TEMP "]'; then
      echo "disabled on $RR"
    else
      echo "[warn] could not disable TEMP peers on $RR"
    fi | head -n 5
  else
    echo "[dry-run] would disable TEMP on $RR"
  fi
done

# For towers, fetch peer list via Oxidized from this network's RR and disable
if [[ -n "$RR_SRC" && -n "$OXIDIZED" ]]; then
  PEERS=$(curl -sf --max-time 30 "${OXIDIZED}/node/fetch/$RR_SRC" 2>&1 | grep -oE 'remote\.address=[0-9.]+' | cut -d= -f2 | sort -u | head -n 300 || true)
  for ip in $PEERS; do
    noc_valid_ipv4 "$ip" || { echo "[warn] skipping non-IPv4 peer '$ip'"; continue; }
    if [[ -n "$PREFIX" && "$ip" == "$PREFIX"* ]]; then
      echo "=== tower $ip ==="
      tower_ssh "$ip" '/routing/bgp/connection print where name~"TEMP "' | head -n 5 || continue
      if [[ -z "$DRYRUN" ]]; then
        tower_ssh "$ip" '/routing/bgp/connection disable [find where name~"TEMP "]' | head -n 5 || true
        echo "Disabled TEMP on $ip"
      else
        echo "[dry-run] would disable TEMP on $ip"
      fi
    fi
  done
fi

echo "Rollback complete. Verify: ssh admin@<RR> '/routing/bgp/connection print where name~TEMP' should show X disabled, session not E."
