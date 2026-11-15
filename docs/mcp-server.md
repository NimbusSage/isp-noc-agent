# MCP Server (Network Troubleshooter)

> Operator manual index lives in the repo root [README](../README.md).

## MCP Server (Network Troubleshooter)

A registered MCP server exposing read-only network diagnostics plus bounded exact-IP inventory lookup, with device credentials encrypted and out of the agent's model context. **Already registered** in `opencode.json` — `run-diagnostics`, `run-exact-device-diagnostics`, `lookup-device-by-exact-ip`, and `list-device-types` are available to the agent automatically. The exact-IP tool queries only the requested LibreNMS device endpoint and reports poll freshness; it does not prove current reachability or site truth. The exact-device tool is a separate single-bound-credential airOS read with no fallback; the tool's input restricts `deviceType` to `ubnt-airos`. Rename-site CPE reads use the isolated `customer-airos` / `customer-mt` pools via `run-diagnostics` (same read-only commands, separate credential lists). No production CPE bindings, trusted inventory resolver, or isolated CPE-secret process are configured. Legacy type-wide `run-diagnostics` fallback remains unchanged for ticket/tower analysis. The temporary-site operator authorization includes temporary donor infrastructure AP interruption and narrowly scoped radio/address/OSPF recovery changes; these run through the gated typed executor (`temp-radio-*`, `temp-ospf-*`, `temp-mt-*` — kill switch, transport flags, host allowlist, `execute:true`, exact-device bindings, live-proven end-to-end). Policy authorization does not replace the executor's gates. Customer CPE credentials and Site Survey are not prerequisites for infrastructure AP inventory discovery or role planning; any individual CPE operation is separate and gated.

**What it does:**
- Connects to MikroTik (SSH/REST), UBNT UISP, UBNT airOS, and UBNT Wave WaveOS SSH (ucfg-show, assoc-stations, iwconfig) devices
- Runs allowlisted **read-only** commands only (system resource, interface stats, ping, etc.), provides a bounded `lookup-device-by-exact-ip` LibreNMS inventory lookup, and exposes the gated temporary-site executor tools (`temp-radio-plan`/`apply`/`rollback`, `temp-ospf-plan`/`apply`/`rollback`, `temp-mt-flip`/`rollback`/`bounce`/`alias`/`alias-remove`, `temp-ap-wds`).
- Read tools (`run-diagnostics`, `run-exact-device-diagnostics`, `lookup-device-by-exact-ip`, `list-device-types`) run only allowlisted read-only commands; every parameter is checked against a strict pattern before use. By default the server connects only to private and loopback addresses (10/8, 172.16/12, 192.168/16, 169.254/16, loopback). `NOC_READ_ALLOWED_HOSTS` replaces that list when set. The host name of a registered service (`site-pack/profile.json` resources, set with `python3 scripts/resources.py set`) is allowed for the device type that uses it, for example the UISP host for `ubnt-uisp`, and for no other device type. Writes go only through typed, gated tools: `routeros-write` supports six operations (`poe-off`, `poe-forced-on`, `poe-auto`, `poe-cycle`, `interface-enable`, `interface-disable`) and `routeros-temp-access` supports five (`temp-address`, `temp-dhcp`, `temp-nat`, `temp-ssh`, `temp-cleanup`) — the temporary-access path a factory-reset restore needs (router address on the factory subnet, a Wave DHCP lease, an airOS NAT relay, SSH port forwarding, cleanup), which previously required an operator-exported router password. Both tools require `NOC_WRITE_ENABLED`, `NOC_WRITE_ROUTEROS_TRANSPORT`, an exact-device credential binding, and a host inside write scope (private, link-local and loopback IPv4 by default; `NOC_WRITE_ALLOWED_HOSTS` replaces that default when set, to narrow it or to add a public management address); without `execute: true` they show only the plan. `librenms-add-device` adds one device to LibreNMS only after an SNMPv2c GET returns `sysName` with the monitoring community (a dependency-free BER client; the community never goes on a command line), and never sets `force_add`, so a duplicate sysName or a failed discovery is reported verbatim instead of overridden. `snmp-config` (`snmp-enable`, `snmp-verify`) is the SNMP provisioning path for devices that answer but are not yet monitored, for three vendors (`mikrotik`, `airos`, `wave` — on WaveOS it writes `services.snmp.community` then `services.snmp.enabled` through `ucfg edit` and persists with `ucfg commit`), writes the org `sysContact` from `NOC_SNMP_CONTACT` on every vendor (never a literal in this repository: the name identifies the organization, so it is configuration; unset leaves `sysContact` as it is), and sets `sysLocation` to this device's own GPS fix as `[lat, lon]` — the caller passes it as `gps` from a read of that device, and with no fix an existing location is left untouched: it applies the operator-standard config on MikroTik — one read-only community with `security=none`, the poller networks from `NOC_SNMP_COMMUNITY_ADDRESSES` in `addresses=` (applied only when that list is configured, since a wrong list silently breaks polling), `trap-community` pointed at it, and a configured `/snmp src-address` reset to `::` because a src-address the device no longer owns stops the daemon answering — and just the community on (`/snmp`) or airOS (`snmp.status` / `snmp.community`, snapshot + softrestart save, no reboot), reads the community only from `NOC_SNMP_COMMUNITY` and redacts it from every result and audit line, and needs the same kill switch and write scope plus its own `NOC_WRITE_SNMP_TRANSPORT=verified`. Because these devices have no per-device binding yet, it may use the device-type credential pool the read path already uses — only when `NOC_WRITE_SNMP_POOL_CREDENTIALS=true`, in priority order, bounded by `NOC_WRITE_SNMP_MAX_POOL_TRIES`. `snmp-verify` is read-only and needs no kill switch. `routeros-temp-access` tags every object it creates with `comment="temp reprogram-device"`, skips a create step when the object already exists, and `temp-cleanup` removes only that tag. A `temp-nat` relay also inserts one tagged `chain=forward action=accept` at the top of the forward chain, scoped to the translated address and port, so the site's forward-chain drop (`securemikrotik` here) can never shadow it; `temp-cleanup` removes that rule with the rest. `temp-ssh` sets `/ip ssh forwarding-enabled=both` so a local `-L` forward (`scripts/ssh_tunnel.py`) reaches the device through the router with no NAT at all; that setting is deliberately left on, matching how most routers here are configured. The `temp-*` tools for temporary sites have their own gates. SSH host key behavior is set by `NOC_SSH_HOSTKEY_MODE` (`off`, `warn`, `tofu`, or `strict`); the default `warn` records keys and logs new or changed ones without blocking.
- An unknown `NOC_SSH_HOSTKEY_MODE` value logs an error and falls back to `strict` behavior; under `tofu`, a key that changes after being recorded is blocked; `strict` also blocks keys that have not been seen before.
- Redacts secrets (passwords, tokens, keys, serials) before returning to the agent; IPs/MACs stay visible per operator policy
- Logs every invocation for audit

**Quick start (first-time setup):**
```bash
cd mcp-server
npm install
npm run build

# Create + encrypt credentials
cp config/credentials.example.json config/credentials.json
# edit credentials.json with your device creds
export NOC_MCP_PASSPHRASE="<your_passphrase>"
npm run encrypt -- config/credentials.json
```

**OpenCode registration** (already in `opencode.json` at the project root):
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "network-troubleshooter": {
      "type": "local",
      "command": ["node", "scripts/mcp_launch.mjs"],
      "enabled": true,
      "environment": {
        "NOC_MCP_PASSPHRASE": "{env:NOC_MCP_PASSPHRASE}"
      }
    }
  }
}
```

The path is relative, so the same file works in every checkout. OpenCode resolves it against the directory it was started in, so start OpenCode from the repository root; `scripts/mcp_launch.mjs` then finds `mcp-server/` on its own.

The `NOC_MCP_PASSPHRASE` env var must be set in the agent harness's process environment (it is — the systemd service loads `.env` via `EnvironmentFile`). The MCP server also self-loads the repo-root `.env` as a fallback for any unset var (`NOC_MCP_DOTENV` override, else `./.env`, else `../.env` relative to its cwd; explicit env always wins — see `mcp-server/src/config/repo-env.ts`). A scrubbed-env smoke test (`env -i … node dist/index.js` + `tools/call list-device-types`) proves the store decrypts. If the passphrase is still missing, the error names the fallback path attempted.

**Universal tool CLI — no MCP client required:** any agent harness or operator shell can invoke every tool above through `mcp-server`'s `tool-cli`, which calls the exact `handleToolCall` entry point the stdio transport uses (same schemas, same five gates, same audit — not a second implementation): `cd mcp-server && npm run tool -- --list`, `npm run tool -- temp-radio-plan '{...}'`, `--args-file`, or `--stdin`. Exit 0 = success, 1 = tool blocked/failed (gate misses fail closed naming the gate), 2 = usage/JSON error. This is what lets a non-OpenCode agent run the temp-site executor automatically under the skill's gates.

The separate `lookup-device-by-exact-ip` tool accepts one IPv4 and queries LibreNMS `GET /api/v0/devices/{ip}` and falls back once to the server-side filtered `GET /api/v0/devices?type=ipv4&query={ip}` when that 404s; a 404 means "no such device" and reports `not_found`, not an integration failure; it reports poll freshness and identity uncertainty; set `inventory.enabled`, `inventory.libreNmsBaseUrl`, and `inventory.libreNmsTokenEnvVar` in `mcp-server/config/server.json`. It reports poll freshness and identity uncertainty. Results corroborate identity/status only, not current reachability or site truth.

For Yankee, the agent and operator will validate commands together: the operator performs one bounded manual change, then the agent reviews sanitized before/after configuration and readback before proceeding. No free-form MCP writes or agent-run raw SSH are available.

Full documentation: [mcp-server/README.md](../mcp-server/README.md)

---

