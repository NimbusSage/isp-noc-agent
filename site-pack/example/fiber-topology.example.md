# Fiber topology — example (all names and IPs are fictional)

Copy to `site-pack/topology/fiber-topology.md` and replace with your own network.
`path-analyzer` and `temp-bgp-peering` read this file to walk a traceroute back to
the fiber router when a ticket carries no usable `Network/Fiber` field. If the file
is absent they fall back to live discovery and say so.

## The walk (edge -> fiber router)

```
hops = traceroute to 8.8.8.8, ordered, only 10.x hops kept
public_idx = index of the first public hop (e.g. 198.51.100.42)
edge_idx   = public_idx - 1                   # the internet edge router
if hops[edge_idx] is in the RDS2216/CGNAT subnet (or its name matches rds|cgnat):
    fiber_idx = edge_idx - 2                  # skip the LibreQoS hop
else:
    fiber_idx = edge_idx - 1
fiber_router = hops[fiber_idx]
```

## Your edge routers

| Name | IP | Notes |
|---|---|---|
| edge-2216-a | 10.0.6.1 | primary edge |
| edge-2216-b | 10.0.6.2 | second edge, dual-homed |

LibreQoS/CGNAT hop subnet (optional): `10.0.30.0/24`.

## Fiber networks

| Network | Fiber router (as named in traceroutes) | Fiber router IP | RR (see `bgp-rr-map.json`) |
|---|---|---|---|
| Alpha | Alpha Fiber | 10.0.20.5 | 10.0.20.1 |
| Bravo | Bravo Fiber | 10.0.21.13 | 10.0.21.33 |
| Charlie | Charlie Fiber | 10.0.8.3 | 10.0.8.119 |
| Eta | Eta Fiber | 10.0.5.1 | (none) |

## Verified example path

`zeta pppoe 10.0.1.1` -> `iota pppoe 10.0.2.1` -> `kappa pppoe 10.0.3.1` -> `10.0.4.1` (transport) -> `Eta Fiber 10.0.5.1` -> `edge-2216-a 10.0.6.1` -> `198.51.100.42` (public).

Record new discoveries (new fiber routers, RDS hops, edge changes) here as you verify them live.
