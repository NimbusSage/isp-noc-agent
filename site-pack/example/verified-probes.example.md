# Verified probes — example (all names and IPs are fictional)

Copy to `site-pack/topology/verified-probes.md` and keep your own known-good probe
output here so the agent can compare a live result against a past healthy one.

## Traceroute from a tower router

```
/tool traceroute address=8.8.8.8 count=1 use-dns=no
 1  10.0.2.1     0%  1  1ms
 2  10.0.3.1     0%  1  2ms
 3  10.0.4.1     0%  1  3ms
 4  10.0.5.1     0%  1  4ms      <- fiber router (Eta Fiber)
 5  10.0.6.1     0%  1  4ms      <- edge (edge-2216-a)
 6  198.51.100.42 0% 1  6ms      <- first public hop
```

## Bandwidth test to the fiber router (healthy baseline)

```
/tool bandwidth-test address=10.0.5.1 user=admin password=<from MCP store> protocol=tcp duration=10 direction=both
status: done  tx-10-second-average: 64.1Mbps  rx-10-second-average: 194.2Mbps  local-cpu-load: 41%
```

## BGP connections on a route reflector

```
/routing/bgp/connection print where disabled=no
 name=2alphabgprr1  remote.address=10.0.200.30  local.role=ibgp-rr  multihop=yes  output.network=bgp-networks
```
