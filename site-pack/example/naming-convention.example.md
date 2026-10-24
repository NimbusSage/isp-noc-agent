# Device naming convention (rename-site) — example, all names fictional

Copy to `site-pack/rename-site/naming-convention.md`. `rename-site` uses this per-type
mapping together with the ticket-context host-order table. Prefix swaps replace only the
site prefix; the suffixes below are preserved verbatim.

| Object type | Pattern | Example (old -> new) |
|---|---|---|
| Tower router | `<site> pppoe` | `omega pppoe` -> `sigma pppoe` |
| Backhaul router | `<site> backhauls` | `omega backhauls` -> `sigma backhauls` |
| Sector AP | `<site>5n` / `5s` / `5e` / `5w` / `5x` | `omega5n` -> `sigma5n` |
| Backhaul radio | `<siteA>2<siteB>AP` / `<siteA>2<siteB>ST` | `omega2quebecAP` -> `sigma2quebecAP` |
| Sector switch | `<site>sectorswitch` | `omegasectorswitch` -> `sigmasectorswitch` |
| MyMate site / map | the site name as operators say it | `Omega` -> `Sigma` |

Notes: keep a column per object type for the live hostname key (`resolv.host.1.name` on
airOS, `system.hostname` on Wave, `/system identity` on RouterOS) and for the SSID, because
the two often differ.
