# MCP Server (Network Troubleshooter)

> Operator manual index lives in the repo root [README](../README.md).

## 12. MCP Server (Network Troubleshooter)

A registered MCP server exposing read-only network diagnostics plus bounded exact-IP inventory lookup, with device credentials encrypted and out of the agent's model context. **Already registered** in `opencode.json` — `run-diagnostics`, `run-exact-device-diagnostics`, `lookup-device-by-exact-ip`, and `list-device-types` are available to the agent automatically. The exact-IP tool queries only the requested LibreNMS device endpoint and reports poll freshness; it does not prove current reachability or site truth. The exact-device tool is a separate single-bound-credential airOS read with no fallback; the tool's input restricts `deviceType` to `ubnt-airos`. Rename-site CPE reads use the isolated `customer-airos` / `customer-mt` pools via `run-diagnostics` (same read-only commands, separate credential lists). No production CPE bindings, trusted inventory resolver, or isolated CPE-secret process are configured. Legacy type-wide `run-diagnostics` fallback remains unchanged for ticket/tower analysis. The temporary-site operator authorization includes temporary donor infrastructure AP interruption and narrowly scoped radio/address/OSPF recovery changes; these run through the gated typed executor (`temp-radio-*`, `temp-ospf-*`, `temp-mt-*` — kill switch, transport flags, host allowlist, `execute:true`, exact-device bindings, live-proven end-to-end). Policy authorization does not replace the executor's gates. Customer CPE credentials and Site Survey are not prerequisites for infrastructure AP inventory discovery or role planning; any individual CPE operation is separate and gated.

**What it does:**
- Connects to MikroTik (SSH/REST), UBNT UISP, UBNT airOS, and UBNT Wave WaveOS SSH (ucfg-show, assoc-stations, iwconfig) devices
- Runs allowlisted **read-only** commands only (system resource, interface stats, ping, etc.), provides a bounded `lookup-device-by-exact-ip` LibreNMS inventory lookup, and exposes the gated temporary-site executor tools (`temp-radio-plan`/`apply`/`rollback`, `temp-ospf-plan`/`apply`/`rollback`, `temp-mt-flip`/`rollback`/`bounce`/`alias`/`alias-remove`, `temp-ap-wds`).
- Read tools (`run-diagnostics`, `run-exact-device-diagnostics`, `lookup-device-by-exact-ip`, `list-device-types`) run only allowlisted read-only commands; every parameter is checked against a strict pattern before use. By default the server only connects to private and loopback addresses (10/8, 172.16/12, 192.168/16, 169.254/16, loopback); `NOC_READ_ALLOWED_HOSTS` replaces that list. Writes go only through typed, gated tools: `routeros-write` supports six operations (`poe-off`, `poe-forced-on`, `poe-auto`, `poe-cycle`, `interface-enable`, `interface-disable`) and requires `NOC_WRITE_ENABLED`, `NOC_WRITE_ROUTEROS_TRANSPORT`, `NOC_WRITE_ALLOWED_HOSTS`, and an exact-device credential binding; without `execute: true` it shows only the plan. The `temp-*` tools for temporary sites have their own gates. SSH host key behavior is set by `NOC_SSH_HOSTKEY_MODE` (`off`, `warn`, `tofu`, or `strict`); the default `warn` records keys and logs new or changed ones without blocking.
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
      "command": ["node", "<path-to-repo>/mcp-server/dist/index.js"],
      "cwd": "<path-to-repo>/mcp-server",
      "enabled": true,
      "environment": {
        "NOC_MCP_PASSPHRASE": "{env:NOC_MCP_PASSPHRASE}"
      }
    }
  }
}
```

The `NOC_MCP_PASSPHRASE` env var must be set in the OpenCode process environment (it is — the systemd service loads `.env` via `EnvironmentFile`). Since the MCP server also self-loads the repo-root `.env` as a fallback for any unset var (`NOC_MCP_DOTENV` override, else `./.env`, else `../.env` relative to its cwd; explicit env always wins — see `mcp-server/src/config/repo-env.ts`). TUI sessions launched from a bare shell therefore work with no manual exports; a scrubbed-env smoke test (`env -i … node dist/index.js` + `tools/call list-device-types`) proves the store decrypts. If the passphrase is still missing, the error names the fallback path attempted.

**Universal tool CLI — no MCP client required:** any agent harness or operator shell can invoke every tool above through `mcp-server`'s `tool-cli`, which calls the exact `handleToolCall` entry point the stdio transport uses (same schemas, same five gates, same audit — not a second implementation): `cd mcp-server && npm run tool -- --list`, `npm run tool -- temp-radio-plan '{...}'`, `--args-file`, or `--stdin`. Exit 0 = success, 1 = tool blocked/failed (gate misses fail closed naming the gate), 2 = usage/JSON error. This is what lets a non-OpenCode agent run the temp-site executor automatically under the skill's gates.

The separate `lookup-device-by-exact-ip` tool accepts one IPv4 and queries only LibreNMS `GET /api/v0/devices/{ip}`; set `inventory.enabled`, `inventory.libreNmsBaseUrl`, and `inventory.libreNmsTokenEnvVar` in `mcp-server/config/server.json`. It reports poll freshness and identity uncertainty. Results corroborate identity/status only, not current reachability or site truth.

For Yankee, the agent and operator will validate commands together: the operator performs one bounded manual change, then the agent reviews sanitized before/after configuration and readback before proceeding. No free-form MCP writes or agent-run raw SSH are available.

Full documentation: [mcp-server/README.md](../mcp-server/README.md)

---

