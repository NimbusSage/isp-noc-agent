#!/usr/bin/env bash
# install.sh — deploy NocAgent at ANY path (no hardcoded install location).
#
#   sudo bash scripts/install.sh                  # install deps + systemd units (+ start the webhook server if .env checks out)
#   sudo bash scripts/install.sh --dry-run        # preflight + print the rendered unit files, change nothing
#   sudo bash scripts/install.sh --no-mcp         # skip the MCP server build
#   sudo bash scripts/install.sh --with-gui       # also `npm ci` the repo-root deps (puppeteer-core, for scripts/wave_gui.mjs)
#   sudo bash scripts/install.sh --enable-backup  # also enable the daily noc-backup.timer (archives .env + credentials)
#   sudo bash scripts/install.sh --no-start       # never start/restart noc-agent, only install + enable it
#   sudo bash scripts/install.sh --protect-tree   # also make the checkout read-only for the service and its agents (takes effect on restart)
#   sudo bash scripts/install.sh --unprotect-tree # remove that restriction again
#   sudo bash scripts/install.sh --node-bin PATH  # node binary baked into the noc-agent unit (default: `node` on PATH, or $NODE_BIN)
#
# The onboarding wizard (agent session: "onboard me") handles .env, OAuth,
# credentials.enc and verification. This script is the machine-level install.
set -euo pipefail
# bash >= 5.2 treats `&` in a ${var//pat/repl} replacement specially; paths may legally contain it.
shopt -u patsub_replacement 2>/dev/null || true

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NO_MCP=0
DRY_RUN=0
WITH_GUI=0
ENABLE_BACKUP=0
NO_START=0
PROTECT_TREE=0
UNPROTECT_TREE=0
NODE_BIN_ARG="${NODE_BIN:-}"

usage() { sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-mcp) NO_MCP=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --with-gui) WITH_GUI=1 ;;
    --enable-backup) ENABLE_BACKUP=1 ;;
    --no-start) NO_START=1 ;;
    --protect-tree) PROTECT_TREE=1 ;;
    --unprotect-tree) UNPROTECT_TREE=1 ;;
    --node-bin)
      [[ $# -ge 2 ]] || { echo "--node-bin needs a path" >&2; exit 2; }
      NODE_BIN_ARG="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

# Keep the original argument list for the sudo re-exec below.
REEXEC_ARGS=()
[[ $NO_MCP -eq 1 ]] && REEXEC_ARGS+=(--no-mcp)
[[ $DRY_RUN -eq 1 ]] && REEXEC_ARGS+=(--dry-run)
[[ $WITH_GUI -eq 1 ]] && REEXEC_ARGS+=(--with-gui)
[[ $ENABLE_BACKUP -eq 1 ]] && REEXEC_ARGS+=(--enable-backup)
[[ $NO_START -eq 1 ]] && REEXEC_ARGS+=(--no-start)
[[ $PROTECT_TREE -eq 1 ]] && REEXEC_ARGS+=(--protect-tree)
[[ $UNPROTECT_TREE -eq 1 ]] && REEXEC_ARGS+=(--unprotect-tree)

DID=()      # what this run actually changed (printed at the end)
SKIPPED=()  # things deliberately left alone, with the reason

fail() { echo "ERROR: $*" >&2; exit 1; }

# ---- 0. preflight ---------------------------------------------------------------------------------
# Resolve node (the binary the service will run) and check python3 BEFORE touching anything.
if [[ -z "$NODE_BIN_ARG" ]]; then
  NODE_BIN_ARG="$(command -v node 2>/dev/null || true)"
fi
[[ -n "$NODE_BIN_ARG" ]] || fail "node not found on PATH. Install Node.js >= 20 (https://nodejs.org) or pass --node-bin /absolute/path/to/node (under sudo, nvm-installed node is not on PATH)."
NODE_BIN="$(readlink -f "$NODE_BIN_ARG" 2>/dev/null || echo "$NODE_BIN_ARG")"
[[ -x "$NODE_BIN" ]] || fail "node binary is not executable: $NODE_BIN"
NODE_MAJOR="$("$NODE_BIN" -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
[[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] || NODE_MAJOR=0
(( NODE_MAJOR >= 20 )) || fail "Node.js >= 20 is required (found $("$NODE_BIN" --version 2>/dev/null || echo 'unknown') at $NODE_BIN). Install a newer Node or pass --node-bin."

PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
[[ -n "$PYTHON_BIN" ]] || fail "python3 not found. Install Python 3.9+ (the helper scripts, metrics and evals are Python)."
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null \
  || fail "Python 3.9+ is required (found $("$PYTHON_BIN" --version 2>&1) at $PYTHON_BIN)."

# Paths end up inside systemd unit files: refuse the few characters that cannot be expressed safely.
for label_value in "install dir=$INSTALL_DIR" "node binary=$NODE_BIN"; do
  label="${label_value%%=*}"; value="${label_value#*=}"
  if [[ "$value" =~ [[:space:]\"\'\\$] ]]; then
    fail "the $label path contains whitespace, a quote, a backslash or '\$', which cannot be written into a systemd unit safely: $value"
  fi
done
# systemd expands % specifiers in unit files; a literal % must be doubled. `|` and `&` need no escaping here.
UNIT_INSTALL_DIR="${INSTALL_DIR//'%'/'%%'}"
UNIT_NODE_BIN="${NODE_BIN//'%'/'%%'}"

if [[ $EUID -ne 0 && $DRY_RUN -eq 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    # Pass the resolved node explicitly: sudo's secure_path usually hides nvm-installed binaries.
    exec sudo bash "$0" "${REEXEC_ARGS[@]}" --node-bin "$NODE_BIN"
  fi
  fail "run as root (systemd units need root; no sudo available)"
fi

if [[ $DRY_RUN -eq 0 ]]; then
  command -v npm >/dev/null 2>&1 || fail "npm not found on PATH (it ships with Node.js; under sudo, nvm-installed npm is not on PATH)."
  command -v systemctl >/dev/null 2>&1 || fail "systemctl not found: this installer targets systemd hosts."
fi

echo "== NocAgent install at: $INSTALL_DIR"
echo "   node:    $NODE_BIN ($("$NODE_BIN" --version))"
echo "   python3: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
[[ $DRY_RUN -eq 1 ]] && echo "   mode:    DRY RUN (nothing will be changed)"

# ---- 1. Webhook server deps -------------------------------------------------------------------------
echo "-- npm ci (server/) ..."
if [[ $DRY_RUN -eq 0 ]]; then
  (cd "$INSTALL_DIR/server" && npm ci --omit=dev)
  DID+=("installed webhook server dependencies (server/: npm ci --omit=dev)")
fi

# ---- 2. MCP server (optional but recommended for network access) -------------------------------------
if [[ $NO_MCP -eq 0 ]]; then
  echo "-- npm ci + build (mcp-server/) ..."
  if [[ $DRY_RUN -eq 0 ]]; then
    (cd "$INSTALL_DIR/mcp-server" && npm ci && npm run build)
    DID+=("installed and built the MCP server (mcp-server/)")
  fi
else
  SKIPPED+=("MCP server build (--no-mcp)")
fi

# ---- 2b. Repo-root deps (puppeteer-core for scripts/wave_gui.mjs) — opt-in only -----------------------
if [[ $WITH_GUI -eq 1 ]]; then
  echo "-- npm ci (repo root: puppeteer-core for wave_gui.mjs) ..."
  if [[ $DRY_RUN -eq 0 ]]; then
    (cd "$INSTALL_DIR" && npm ci --omit=dev)
    DID+=("installed repo-root dependencies (puppeteer-core) because of --with-gui")
  fi
else
  SKIPPED+=("repo-root npm deps / puppeteer-core (pass --with-gui if you use scripts/wave_gui.mjs)")
fi

# ---- 3. systemd units from templates (paths templated, never hardcoded) -------------------------------
echo "-- generating systemd units for $INSTALL_DIR ..."
UNIT_NAMES=(noc-agent.service noc-backup.service noc-metrics.service noc-evals.service noc-treeguard.service noc-backup.timer noc-metrics.timer noc-evals.timer noc-treeguard.timer)

render_unit() {
  local tpl out
  tpl="$(cat "$1")" || return 1
  out="${tpl//'{{INSTALL_DIR}}'/"$UNIT_INSTALL_DIR"}"
  out="${out//'{{NODE_BIN}}'/"$UNIT_NODE_BIN"}"
  if [[ "$out" == *'{{'* ]]; then
    echo "ERROR: unresolved placeholder left in $1" >&2
    return 1
  fi
  printf '%s\n' "$out"
}

RENDER_DIR="$(mktemp -d)"
trap 'rm -rf "$RENDER_DIR"' EXIT
for name in "${UNIT_NAMES[@]}"; do
  [[ -f "$INSTALL_DIR/deploy/$name.template" ]] || fail "missing template deploy/$name.template"
  render_unit "$INSTALL_DIR/deploy/$name.template" > "$RENDER_DIR/$name"
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "----- $name -----"; cat "$RENDER_DIR/$name"
  fi
done

if [[ $PROTECT_TREE -eq 1 && $UNPROTECT_TREE -eq 1 ]]; then fail "--protect-tree and --unprotect-tree cannot be combined"; fi
DROPIN_DIR=/etc/systemd/system/noc-agent.service.d
DROPIN_NAME=10-protect-tree.conf
if [[ $PROTECT_TREE -eq 1 ]]; then
  [[ -f "$INSTALL_DIR/deploy/noc-agent.protect-tree.conf.template" ]] || fail "missing template deploy/noc-agent.protect-tree.conf.template"
  render_unit "$INSTALL_DIR/deploy/noc-agent.protect-tree.conf.template" > "$RENDER_DIR/$DROPIN_NAME"
  if [[ $DRY_RUN -eq 1 ]]; then echo "----- noc-agent.service.d/$DROPIN_NAME -----"; cat "$RENDER_DIR/$DROPIN_NAME"; fi
fi

if command -v systemd-analyze >/dev/null 2>&1; then
  # Advisory only: `verify` also resolves ExecStart binaries and EnvironmentFile, which may legitimately not exist yet.
  if ! (cd "$RENDER_DIR" && systemd-analyze verify "$RENDER_DIR"/*.service >"$RENDER_DIR/verify.txt" 2>&1); then
    echo "   (systemd-analyze verify reported notes; showing them, not fatal)"
    sed 's/^/   | /' "$RENDER_DIR/verify.txt" | head -20
  fi
fi

# Never start the service on a config that the server itself would refuse.
CONFIG_OK=0
if [[ -f "$INSTALL_DIR/.env" ]]; then
  echo "-- checking .env with: node server/index.js --check-config (no values are printed) ..."
  if (cd "$INSTALL_DIR/server" && "$NODE_BIN" index.js --check-config); then
    CONFIG_OK=1
  fi
else
  echo "-- .env missing: skipping --check-config"
fi

if [[ $DRY_RUN -eq 1 ]]; then
  if [[ $CONFIG_OK -eq 1 ]]; then echo "(dry-run: .env passes --check-config; a real run would start noc-agent)";
  else echo "(dry-run: a real run would NOT start noc-agent until .env passes --check-config)"; fi
  echo "(dry-run — nothing written)"
  exit 0
fi

for name in "${UNIT_NAMES[@]}"; do
  install -m 0644 "$RENDER_DIR/$name" "/etc/systemd/system/$name"
done
if [[ $PROTECT_TREE -eq 1 ]]; then
  # The writable folders must exist before the unit starts, or its mount namespace cannot be set up.
  install -d -m 0755 "$INSTALL_DIR/logs" "$INSTALL_DIR/mcp-server/logs" "$INSTALL_DIR/mcp-server/state"
  install -d -m 0755 "$DROPIN_DIR"
  install -m 0644 "$RENDER_DIR/$DROPIN_NAME" "$DROPIN_DIR/$DROPIN_NAME"
  DID+=("installed $DROPIN_DIR/$DROPIN_NAME: the checkout is read-only for noc-agent and its agents after the next restart")
elif [[ $UNPROTECT_TREE -eq 1 ]]; then
  if [[ -f "$DROPIN_DIR/$DROPIN_NAME" ]]; then
    rm -f "$DROPIN_DIR/$DROPIN_NAME"; rmdir "$DROPIN_DIR" 2>/dev/null || true
    DID+=("removed $DROPIN_DIR/$DROPIN_NAME: noc-agent can write the checkout again after the next restart")
  else
    SKIPPED+=("--unprotect-tree: $DROPIN_DIR/$DROPIN_NAME was not installed")
  fi
fi
DID+=("wrote ${#UNIT_NAMES[@]} unit files to /etc/systemd/system/ (rendered for $INSTALL_DIR, node=$NODE_BIN)")

# ---- 4. enable ----------------------------------------------------------------------------------------
systemctl daemon-reload
systemctl enable --now noc-metrics.timer noc-evals.timer noc-treeguard.timer
DID+=("enabled and started noc-metrics.timer, noc-evals.timer and noc-treeguard.timer")
if [[ $ENABLE_BACKUP -eq 1 ]]; then
  systemctl enable --now noc-backup.timer
  DID+=("enabled and started noc-backup.timer (--enable-backup)")
else
  SKIPPED+=("noc-backup.timer installed but NOT enabled: it archives .env + credentials.enc (re-run with --enable-backup, or: systemctl enable --now noc-backup.timer)")
fi

systemctl enable noc-agent.service
DID+=("enabled noc-agent.service")
if [[ $NO_START -eq 1 ]]; then
  SKIPPED+=("noc-agent start/restart (--no-start)")
elif [[ $CONFIG_OK -ne 1 ]]; then
  SKIPPED+=("noc-agent NOT started: .env is missing or failed --check-config (fix it, then: systemctl start noc-agent && python3 scripts/doctor.py)")
elif systemctl is-active --quiet noc-agent.service; then
  SKIPPED+=("noc-agent is already running and was left alone; apply the new unit/code with: systemctl restart noc-agent")
else
  systemctl start noc-agent.service
  DID+=("started noc-agent.service (.env passed --check-config)")
fi

echo
echo "== done. What this run did:"
for line in "${DID[@]}"; do echo "   + $line"; done
if [[ ${#SKIPPED[@]} -gt 0 ]]; then
  echo "== deliberately NOT done:"
  for line in "${SKIPPED[@]}"; do echo "   - $line"; done
fi
echo
echo "verify with:  python3 scripts/doctor.py   (and: systemctl status noc-agent)"
