# NOC Network Troubleshooter MCP Server

Secure MCP tools for read-only network device diagnostics, bounded exact-IP inventory corroboration, and the gated temporary-site executor (live-proven end-to-end on INC-A-01). Every tool is also callable from any shell or agent harness via the universal CLI (`npm run tool -- <tool> '<json>'`) — same handler, same gates, no MCP client required.

## Quick Start

1. **Install dependencies:**
   ```bash
   cd mcp-server
   npm install
   ```

2. **Create credential store:**
   ```bash
   # Edit config/credentials.example.json with your device types and credentials
   cp config/credentials.example.json config/credentials.json
   
   # Encrypt it
   NOC_MCP_PASSPHRASE="your-secure-passphrase" npm run encrypt -- config/credentials.json
   ```

3. **Set environment variables:**
   ```bash
   export NOC_MCP_PASSPHRASE="your-secure-passphrase"
   ```

4. **Build and run:**
   ```bash
   npm run build
   node dist/index.js
   ```

### Credential Store Format

Legacy diagnostics credentials remain organized by **device type** and continue using their existing bounded priority fallback for tower/router workflows. Customer CPE passwords live in the isolated `customer-airos` / `customer-mt` pools (same read-only commands as `ubnt-airos` / `mikrotik-ssh`, separate credential lists — see `rename-site` skill); never add a customer secret to an infra pool. The optional `exactDeviceCredentials` format and separate tool are implemented for future CPE reads, but production customer secrets must not be provisioned until an isolated helper validates trusted inventory ID↔host and prevents root-run `noc-agent` from accessing CPE credentials. Once operationally approved, exact lookups require `ubnt-airos` + stable ID + host binding, use one credential attempt, and never fall back to the type-wide list.

```json
{
  "version": 1,
  "deviceTypes": [
    {
      "deviceType": "mikrotik-ssh",
      "credentials": [
        { "name": "admin-primary", "username": "admin", "password": "pass1", "priority": 10 },
        { "name": "admin-backup", "username": "admin", "password": "pass2", "priority": 5 },
        { "name": "readonly", "username": "readonly", "password": "pass3", "priority": 0 }
      ],
      "policy": { "maxAttempts": 3, "timeoutMs": 30000, "requireReadOnly": true }
    },
    {
      "deviceType": "ubnt-uisp",
      "credentials": [
        { "name": "api-token", "username": "admin", "password": "pass", "apiToken": "YOUR_TOKEN", "priority": 0 }
      ]
    },
    {
      "deviceType": "ubnt-airos",
      "credentials": [
        { "name": "default", "username": "ubnt", "password": "ubnt", "priority": 0 }
      ]
    }
  ],
  "exactDeviceCredentials": [
    {
      "deviceType": "ubnt-airos",
      "deviceId": "uisp-device-id-or-normalized-mac",
      "host": "192.0.2.25",
      "credential": { "name": "cpe-binding", "username": "ubnt", "password": "<stored-secret>", "priority": 0, "tags": ["customer-cpe"] }
    }
  ]
}
```

Use synthetic values above as documentation only; never commit real customer passwords in the tracked example. Existing encrypted stores without `exactDeviceCredentials` load as an empty list and retain all legacy behavior.

**Key concepts:**
- **`deviceType`** — One of: `mikrotik-ssh`, `mikrotik-rest`, `ubnt-uisp`, `ubnt-airos`, `ubnt-wave`, plus isolated customer pools `customer-airos` / `customer-mt` (same read-only commands as `ubnt-airos` / `mikrotik-ssh`; separate credential pools for rename-site CPE reads)
- **`credentials`** — Ordered list of credential pairs; server tries highest priority first
- **`policy.maxAttempts`** — Max credential pairs to try before giving up (default: 5)
- **`policy.timeoutMs`** — Connection timeout per attempt (default: 30000)

**Credential rotation:** Legacy tower/router type-level credentials retain their current bounded priority fallback. Rotate an exact-device CPE binding by replacing the single credential associated with that stable ID and host; never add a customer's secret to a shared `ubnt-airos` fallback pool.

The MCP still has **no Site Survey command**. Exact-device credentials are not authorization to run airView or another potentially disruptive scan; per-model/firmware survey validation and an allowlisted safe operation are separate prerequisites.

## OpenCode integration

`opencode.json` registers the server under the `mcp` key with the name `network-troubleshooter`. The entry uses `"type": "local"`, a `command` array, a `cwd`, and an `environment` map that passes values by name with `{env:NAME}` placeholders:

```json
{
  "mcp": {
    "network-troubleshooter": {
      "type": "local",
      "command": ["node", "/path/to/NocAgent/mcp-server/dist/index.js"],
      "cwd": "/path/to/NocAgent/mcp-server",
      "environment": {
        "NOC_MCP_PASSPHRASE": "{env:NOC_MCP_PASSPHRASE}"
      }
    }
  }
}
```

The server finds its config, credential store, and audit log relative to the folder it starts in. The `cwd` must point to the `mcp-server` folder.

To write this entry with real paths for your checkout:

```bash
python3 scripts/harness_setup.py --harness opencode --write
```

Other coding agents are covered in `docs/harnesses.md`.

Build the server once before first use:

```bash
cd mcp-server && npm ci && npm run build
```

## Provisioning Exact-Device Bindings (operator-approved)

Two binding classes share one format (`deviceType` + stable `deviceId` + `host` → one credential) with different secrets handling:

- **Infra radios (towers/backhaul):** the password is the already-configured pool standard — pinning it to an exact ID+host introduces no new secret. Provision with `node scripts/provision_exact_binding.mjs --device-type ubnt-airos --device-id <id> --host <ip> --from-pool <pool-name>` (dry-run by default; `--apply` writes with a timestamped backup). The executor requires this binding; pool credentials are never used for writes. Remove with `--remove ... --apply` after the incident/window.
- **Customer CPE:** do NOT provision long-term bindings (root-run agent can read the shared store). Per-incident operator-held flow only — see the `temp-site-connection` skill. Validate staged files with `node scripts/cpe_binding_validate.mjs` (never prints the password).

## Universal tool CLI (any harness, no MCP client required)

`src/tool-cli.ts` wraps the exact `NocMcpServer.handleToolCall` entry point the stdio transport uses, so schemas, the five executor gates, bindings, audit, and redaction behave identically. Not a second implementation — no device logic lives in the CLI.

```bash
cd mcp-server && npm run build
npm run tool -- --list
node dist/tool-cli.js temp-radio-plan '{"op":"set-mode","incidentId":"<incidentId>","host":"<host>","deviceId":"<deviceId>","mode":"managed","ssid":"<ssid>"}'
node dist/tool-cli.js temp-ospf-plan --args-file /tmp/ospf-args.json
 echo '{"incidentId":"INC-F-01",...}' | node dist/tool-cli.js temp-ospf-plan --stdin
```

Same repo-root `.env` fallback as the MCP server (`NOC_MCP_DOTENV` override, else `./.env`, else `../.env`; explicit env wins). Tool text goes to stdout; exit 0 = success, 1 = tool blocked/failed (gate failures fail closed naming the gate), 2 = CLI usage/JSON error. Verified live: `temp-radio-plan`, `temp-ospf-plan`, and `temp-radio-apply execute:false` dry-runs plus a binding-gate refusal (`execute:true` with no binding → blocked, exit 1, no connection attempted).

## Available Commands

### `run-diagnostics`

Run a predefined diagnostic command on a network device.

**Parameters:**
- `host` (required) — Target device IP address
- `deviceType` (required) — Device type: `mikrotik-ssh`, `mikrotik-rest`, `ubnt-uisp`, `ubnt-airos`, `customer-airos`, `customer-mt`
- `command` (required) — Command name (see below)
- `params` (optional) — Command-specific parameters

**Example:**
```json
{
  "host": "192.168.1.1",
  "deviceType": "mikrotik-ssh",
  "command": "system-resource",
  "params": {}
}
```

### `run-exact-device-diagnostics`

Separate, allowlisted read-only CPE diagnostic entry point. The MCP schema restricts `deviceType` to `ubnt-airos`; it requires `deviceId`, `host`, `deviceType`, and an existing read-only `command`. The ID and host must both match exactly one encrypted binding. Duplicate/missing bindings fail closed. It makes one authentication attempt and does not log or return the password. It does not alter the behavior of `run-diagnostics` used by existing router/path-analysis workflows.

The supplied `deviceId` and `host` are caller inputs, not authenticated inventory proof. This binding prevents accidental type-wide password fallback; it does not by itself prevent a caller from mislabeling another reachable host. Production CPE invocation is blocked until a trusted helper validates stable ID↔host against UISP/inventory and enforces the permitted host/network boundary; do not treat a direct model-supplied ID+host as sufficient authorization.

### `temp-ospf-plan`

Read-only Temp OSPF planner for the `temp-site-connection` workflow. Validates incident ID, interface, `/30` host-within-network, private space, cost is exactly `1000` (same fiber drain) or `10000` (cross-drain, anything else rejected), and ROS6/ROS7 command shape, then returns tagged apply/rollback RouterOS commands plus a preflight checklist. **Never connects to devices and never executes writes.** 

### `temp-radio-plan`

Read-only typed planner for UBNT station-flip / frequency / reboot ops. Validates incident ID, exact IPv4 host, device ID, op-specific params (mode/ssid, frequency+previous, confirm deadline 60–1800s) and returns exact apply/verify/rollback steps. **Never connects or executes.**

### `temp-radio-apply`

Gated executor (airOS SSH: anchored sed on allowlisted `/tmp/system.cfg` keys, re-read verify, operator-verified `/usr/etc/rc.d/rc.softrestart save`, snapshot + rollback). Requires ALL of: `execute:true`, `TEMP_SITE_WRITE_ENABLED=true`, `TEMP_SITE_AIROS_TRANSPORT=verified`, host in `TEMP_SITE_ALLOWED_HOSTS`, and exactly one exact-device credential binding for the ID+host (type-wide credentials are never used for writes). Snapshots persist owner-only for `temp-radio-rollback` (the safe-mode counterpart on a missed confirm deadline). 

### `temp-radio-rollback`

Restore the owner-only pre-apply snapshot for an incident+host (SFTP put + softrestart save). Same gates as apply. If rollback itself fails, the snapshot stays on file and the operator is escalated to a tech with wired-side access.

### `lookup-device-by-exact-ip`

Read-only exact IPv4 lookup in LibreNMS using `GET /api/v0/devices/{ip}`. It makes one bounded request (no bulk-list fallback), returns identity fields plus source/retrieval time and poll freshness, and asserts a match only when the endpoint returns exactly one device whose hostname equals the queried IP. Stale, future-dated, missing or timezone-unverified poll times, ambiguous results, and hostname mismatches are explicit. LibreNMS-reported status is not proof of current reachability, site truth, radio role, RF compatibility, or topology. Configure `inventory.enabled`, `inventory.libreNmsBaseUrl`, and `inventory.libreNmsTokenEnvVar` in `config/server.json`; default token variable is `LIBRENMS_TOKEN`.

### `list-device-types`

List all available device types, their credential counts, and supported commands.

**Parameters:** None

**Returns:** Array of device types with credential counts and available commands.

### Commands by device type

The commands each device type may run are defined in `mcp-server/src/commands/allowlist.ts`. Every parameter is checked against a strict pattern before it is used.

The available device types are `mikrotik-ssh`, `mikrotik-rest`, `ubnt-uisp`, `ubnt-airos`, `ubnt-wave`, and the isolated customer pools `customer-airos` and `customer-mt`.

Run `list-device-types` to see the full list. It returns every device type with its credential count and its supported commands.

## Temporary-site tools

The temporary-site executor is live-proven end to end.

The read-only planners are `temp-ospf-plan` and `temp-radio-plan`.

The gated executors are `temp-radio-apply` and `temp-radio-rollback` (airOS over SSH), `temp-ospf-apply` and `temp-ospf-rollback` (RouterOS), and `temp-mt-flip`, `temp-mt-rollback`, `temp-mt-bounce`, `temp-mt-alias`, `temp-mt-alias-remove`, and `temp-ap-wds`.

Every write is off by default. All five of the following are required: `execute: true` in the call; `TEMP_SITE_WRITE_ENABLED` in the environment; the transport flag for the device family (`TEMP_SITE_AIROS_TRANSPORT`, `TEMP_SITE_ROUTEROS_TRANSPORT`, or `TEMP_SITE_MT_TRANSPORT`); `TEMP_SITE_ALLOWED_HOSTS` listing the target host; and an exact-device credential binding for the ID and host.

Rollback snapshots are stored owner-only in `mcp-server/state/temp-site-snapshots`. Set `TEMP_SITE_SNAPSHOT_DIR` to override the path.

Keep `run-diagnostics` read-only. Do not pass commands from prompts through to a device.


## Security Features

- **Encrypted credentials:** AES-256-GCM encrypted store, passphrase in env var only
- **Command allowlisting:** Only predefined read-only commands accepted
- **Output redaction:** passwords, tokens, keys, serials, non-allowlisted hostnames automatically redacted; IPs/MACs left visible (operator policy)
- **Audit logging:** Every invocation logged without secrets
- **Bounded retries:** Existing type-level diagnostic attempts remain policy-bounded; exact-device CPE diagnostics make one bound-credential attempt with no fallback
- **Fail closed:** Unknown device/command/input → reject immediately
- **Credential rotation:** Legacy type-level profiles retain bounded fallback; exact-device CPE bindings use a separate one-device/one-credential path with no spraying or cross-device fallback
