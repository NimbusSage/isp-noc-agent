# T1 reference - devices (MCP server, encrypted store, LibreNMS)

T1 turns on the read-only device tools: live traceroute and bandwidth-test triage
(`path-analyzer`), wireless link checks (`wireless-health`), LibreNMS lookups, UISP and
airOS reads. Writes are a separate, gated, default-off path (see `docs/governance.md`).

## 1. Build

```bash
cd mcp-server && npm ci && npm run build
node dist/tool-cli.js --list        # proof: tool names print
```
`npm ci` needs the lockfile and node 20+. A build error is almost always a node that is
too old; check `node --version`.

## 2. The encrypted device store

How it works: `config/credentials.enc` (inside `mcp-server`) is AES-256-GCM with a key
derived from `NOC_MCP_PASSPHRASE` (format NOCENC2: scrypt, random salt). The server and
`tool-cli.js` decrypt it at start using the passphrase from the environment or the
repo-root `.env`. The plaintext `credentials.json` is only an input to `encrypt`.

Operator steps (the agent never opens either file):
1. In an editor: `cp mcp-server/config/credentials.example.json mcp-server/config/credentials.json`
   and replace each `CHANGE_ME`. Structure: a `deviceTypes` list; each entry has a
   `deviceType` (`mikrotik-ssh`, `ubnt-uisp`, `ubnt-airos`, `mikrotik-rest`, and the
   isolated pools `customer-airos` and `customer-mt` if present), a `credentials`
   list (`name`, `username`, `password`, optional `apiToken`, `priority`, `tags`) and a
   `policy` (`maxAttempts`, `timeoutMs`, `requireReadOnly`). Credentials of one type
   are tried highest priority first until one works. Delete device types you do not
   use instead of leaving placeholders. Keep the file valid JSON.
2. Passphrase: the agent may run `python3 scripts/env_set.py --generate NOC_MCP_PASSPHRASE`
   (value never printed). The operator copies it from `.env` with their editor into a
   password manager. Lose it and the store cannot be opened.
3. Encrypt, in the operator's own terminal from the repo root:
   ```bash
   set -a; . <(grep '^NOC_MCP_PASSPHRASE=' .env); set +a
   cd mcp-server && npm run encrypt -- config/credentials.json
   ```
   Output: `Encrypted credentials written to config/credentials.enc`. The script
   requires `NOC_MCP_PASSPHRASE` in its process environment (it does not read `.env`
   itself), hence the export. The plaintext is NOT deleted.
4. Remove the plaintext: `shred -u config/credentials.json` (or `rm`). Both files are
   gitignored, but a plaintext password file on disk is the exact thing to avoid.
   Agent check: `ls mcp-server/config` shows `credentials.enc`, not `credentials.json`.
5. To change a credential later: recreate `credentials.json` from the example (or a
   password manager copy), encrypt again with the same passphrase, delete the plaintext.

## 3. Settings

| Key | Secret? | Purpose |
|---|---|---|
| `NOC_MCP_PASSPHRASE` | yes | decrypts the device store |
| `LIBRENMS_URL` | no | for example `http://10.0.x.y`; sysName, hardware and location lookups |
| `LIBRENMS_TOKEN` | yes | LibreNMS API token (`X-Auth-Token`), read access |
| `BTEST_PASSWORD` | yes | RouterOS bandwidth-test password the path analyzer passes to `tool bandwidth-test` |
| `NOC_READ_ALLOWED_HOSTS` | no | read-host policy, below |
| `EDGE_ROUTER_IPS`, `RDS2216_SUBNET` | no | fiber-edge hints for the path walk; usually filled from the site pack |
| `MYMATE_URL`, `MYMATE_USER`, `MYMATE_PASS` | pass: yes | optional MyMate monitor |

Secrets: `python3 scripts/env_set.py NOC_MCP_PASSPHRASE LIBRENMS_TOKEN BTEST_PASSWORD`
run by the operator (already-set keys can be skipped; the tool reports what is
missing with `--list-missing --tier 1`).

### Read-host policy

By default the device tools connect only to private (10/8, 172.16/12, 192.168/16),
loopback and link-local addresses, so a prompt-injected agent cannot aim the credential
pool at a public host. `NOC_READ_ALLOWED_HOSTS` replaces that default when set: a
comma list of exact IPs and CIDRs, for example
`10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,YOUR_CGNAT_RANGE,198.51.100.0/24`. Include the
private ranges again if you still want them. A malformed entry blocks all connections
until fixed. Needed for CGNAT or public-addressed management gear.
Hostnames are never resolved; they match only if listed verbatim.

## 4. Verify

`python3 scripts/doctor.py --tier 1`
- `[PASS] mcp_store  decrypt-ok devicetypes=N` proves the store decrypts.
- `[PASS] librenms ...` proves the token (first LibreNMS call can take about 40 s).
- `[PASS] mymate ...` only if configured.

## 5. First read-only smoke test

Ask for one router address the operator owns (inside the allowed hosts):
```bash
cd mcp-server
node dist/tool-cli.js list-device-types '{}'
node dist/tool-cli.js run-diagnostics '{"host":"10.0.x.y","deviceType":"mikrotik-ssh","command":"system-resource"}'
```
Proof: exit 0 and version, uptime and CPU in the output. The allowlist is read-only
(`system-resource`, `ip-route`, `interface-list`, `ping`, and so on; the full list is in
`docs/mcp-server.md`). Do not use raw SSH from the agent.

## T1 failures

| Symptom | Cause | Fix |
|---|---|---|
| `[  -  ] mcp_store  NOC_MCP_PASSPHRASE not set` | key missing | `--generate` or `env_set.py` |
| `[  -  ] mcp_store  credentials.enc does not exist yet` | not encrypted yet | steps 2.1 to 2.4 |
| `[WARN] mcp_store  mcp-server not built` | no `dist/` | step 1 |
| `[FAIL] mcp_store` after correct build | passphrase differs from the one used to encrypt | re-encrypt with the current passphrase |
| `Host ... is not permitted by NOC_READ_ALLOWED_HOSTS` | address outside the policy | set the variable (above) |
| all credentials fail for a device | wrong password or priority order | fix the plaintext, re-encrypt; `mikrotik-ssh` creds are tried in priority order |
| `Set NOC_MCP_PASSPHRASE environment variable` from encrypt | export step skipped or ran in another shell | repeat the `export` in the same terminal |
