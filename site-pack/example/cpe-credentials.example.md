# CPE credential pool conventions (rename-site) — example, no secrets

Copy to `site-pack/rename-site/cpe-credentials.md`. Describe only HOW customer-device
credentials are organized, never the passwords themselves.

- Customer devices use the isolated MCP pools `customer-airos` (Ubiquiti airOS) and
  `customer-mt` (MikroTik). Infrastructure pools (`ubnt-airos`, `ubnt-wave`,
  `mikrotik-ssh`) are never used for customer devices, and vice versa.
- The passwords live only in the encrypted MCP store (`mcp-server/config/credentials.enc`);
  this file lists pool names, priority order and which vintage of CPE each entry covers.
- Different CPEs accept different pool entries: try them in priority order, one attempt each,
  and stop on the first success. A first reject means "try the next entry", not "wrong password".
- Writes to CPEs use exact-device bindings only; type-wide pool fallback is read-only.
