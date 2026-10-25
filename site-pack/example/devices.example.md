# Device inventory — example (all names and IPs are fictional)

Copy to `site-pack/devices.md` (untracked) and list the devices the agent is expected to
reach. Make sure the MCP credential priorities (`mcp-server/config/credentials.json`
-> encrypted store) cover every row. The agent treats this file as a lookup aid, not as
proof of live state: always verify with a live read.
Service addresses (UISP, LibreNMS, MyMate, Oxidized) are not listed in this table; register them with `python3 scripts/resources.py set <name> <url>`.

| Device | Role | Management IP | Credential pool | Notes |
|---|---|---|---|---|
| zeta pppoe | tower router (PPPoE concentrator) | 10.0.1.1 | mikrotik-ssh | example tower |
| zeta backhauls | backhaul router | 10.0.1.2 | mikrotik-ssh | |
| Eta Fiber | fiber router | 10.0.5.1 | mikrotik-ssh | btest target for the Eta network |
| edge-2216-a | internet edge router | 10.0.6.1 | mikrotik-ssh | |
| 2alphabgprr1 | BGP route reflector | 10.0.20.1 | mikrotik-ssh | see `topology/bgp-rr-map.json` |
