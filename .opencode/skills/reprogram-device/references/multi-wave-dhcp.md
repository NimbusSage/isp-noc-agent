# Several factory-reset Waves on one gateway: one DHCP server per radio

Moved out of `SKILL.md` Step 3e-2. Uses the same operator-led raw-SSH exception and the `$MT_SSH` shorthand defined in `SKILL.md` ("Access rules"); every temporary entry carries `comment="temp reprogram-device"`.

When restoring two or more factory-reset waves on the same gateway, give each radio its own subnet and its own pool/server name. Do not stack multiple radios on one `192.168.1.0/24`: a factory-reset wave self-assigns `192.168.1.20`, and two of them on one segment collide on the same address and neither is reliably reachable.

Allocate the Nth wave to `192.168.N.0/24` (N = 1, 2, 3, ...) with pool and server `waveDHCP` for the first, then `waveDHCP2`, `waveDHCP3`, and so on:

| Wave | Subnet | Router address | Pool and server name | Lease |
|------|--------|----------------|----------------------|-------|
| 1st | `192.168.1.0/24` | `192.168.1.1` | `waveDHCP` | `192.168.1.100` |
| 2nd | `192.168.2.0/24` | `192.168.2.1` | `waveDHCP2` | `192.168.2.100` |
| 3rd | `192.168.3.0/24` | `192.168.3.1` | `waveDHCP3` | `192.168.3.100` |

```bash
# 2nd radio on ether5
$MT_SSH admin@<gateway> '/ip address add address=192.168.2.1/24 interface=ether5 comment="temp reprogram-device"'
$MT_SSH admin@<gateway> '/ip pool add name=waveDHCP2 ranges=192.168.2.100 comment="temp reprogram-device"'
$MT_SSH admin@<gateway> '/ip dhcp-server add address-pool=waveDHCP2 interface=ether5 lease-time=5m name=waveDHCP2 comment="temp reprogram-device"'
$MT_SSH admin@<gateway> '/ip dhcp-server network add address=192.168.2.0/24 gateway=192.168.2.1 comment="temp reprogram-device"'
```

The `gateway=` on the dhcp-server network entry must match that wave's own router address, or the radio leases an IP and then sends its traffic to the wrong gateway.

Poll leases per server, not globally, otherwise you cannot tell which radio is which. Use the tilde `~` (regex match), not `=`: `where server=waveDHCP` is an exact string compare and silently skips `waveDHCP2` and `waveDHCP3`, making a working multi-wave setup look like nothing leased.

```bash
$MT_SSH admin@<gateway> '/ip dhcp-server lease print detail where server~"waveDHCP"'
```

Always confirm with the tilde form before concluding a radio failed to lease. Identify each lease by the `interface=` the server is bound to plus the `mac-address=`, and confirm it against the MAC seen on that port in `/ip neighbor print`. Confirm the model with `host-name=` (factory waves report, for example, `Wave Long-Range`).

Restoring several waves: running them in parallel is fine once each has its own subnet, but verify one end to end before starting the next if the radios share a switch or power domain. A single power event then takes out every in-flight restore at once and you lose the ability to attribute the failure. Otherwise sequence them.
