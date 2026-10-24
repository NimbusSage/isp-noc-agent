# Device inventory — example (all names and IPs are fictional)

Copy to `site-pack/devices.md` (untracked) and list the devices the agent is expected to
reach. Make sure the MCP credential priorities (`mcp-server/config/credentials.json`
-> encrypted store) cover every row. The agent treats this file as a lookup aid, not as
proof of live state: always verify with a live read.

| Device | Role | Management IP | Credential pool | Notes |
|---|---|---|---|---|
| zeta pppoe | tower router (PPPoE concentrator) | 10.0.1.1 | mikrotik-ssh | example tower |
| zeta backhauls | backhaul router | 10.0.1.2 | mikrotik-ssh | |
| Eta Fiber | fiber router | 10.0.5.1 | mikrotik-ssh | btest target for the Eta network |
| edge-2216-a | internet edge router | 10.0.6.1 | mikrotik-ssh | |
| 2alphabgprr1 | BGP route reflector | 10.0.20.1 | mikrotik-ssh | see `topology/bgp-rr-map.json` |
| uisp | UISP controller | 10.0.0.10 | ubnt-uisp | API token in the encrypted store |
| librenms | LibreNMS | 10.0.0.5 | (token in `.env`) | corroboration only |
| mymate | MyMate monitor | 10.0.0.7 | (login in `.env`) | read-mostly |
| oxidized | config backups | 10.0.0.6 | (none) | `GET :8888/node/fetch/<ip>` |
