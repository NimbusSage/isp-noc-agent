# RouterOS writes through `routeros-write` (PoE, interface enable/disable)

The read-only MCP path (`run-diagnostics`) cannot change a router. Router writes happen only through the gated `routeros-write` MCP tool (PoE, interface enable/disable) or the gated `routeros-temp-access` tool (temp access for factory-reset recovery), each callable as `node mcp-server/dist/tool-cli.js <tool> '<json>'` without MCP — or through the typed `temp-*` executors of `temp-site-connection`. Never through improvised SSH from an agent prompt, and never with a password copied out of `credentials.json`. The one raw-SSH exception is the operator-led radio hop in `SKILL.md` ("Access rules"); router work needs no operator password.

## Closed op set

`routeros-write`: `poe-off`, `poe-forced-on`, `poe-auto`, `poe-cycle`, `interface-enable`, `interface-disable`.

`routeros-temp-access` (the temp-access path that used to be operator-led raw SSH — same gates, same exact-device binding):

| op | what it writes |
|---|---|
| `temp-address` | `/ip address add <address>/24` on the interface facing the reset device |
| `temp-dhcp` | the address + `/ip pool` + `/ip dhcp-server` + `/ip dhcp-server network` for a factory-reset Wave |
| `temp-nat` | srcnat masquerade for the temp subnet, plus an optional dstnat relay to `192.168.1.20:22` **and** one tagged `chain=forward action=accept place-before=0` scoped to the translated address/port, so a site forward-chain drop cannot shadow the relay |
| `temp-ssh` | `/ip ssh set forwarding-enabled=both` so `ssh -L` (via `scripts/ssh_tunnel.py`) can reach the device with no NAT at all; **left on** — most routers here already have it, so it is a setting, not debris |
| `temp-cleanup` | removes every object carrying `comment="temp reprogram-device"`, in dependency order |

Every object it creates is tagged `comment="temp reprogram-device"`. Each create step probes first (`unless`) and is skipped when the object already exists, so re-running is safe and a leftover from a prior job is reused. Cleanup removes only that tag: a pre-existing `192.168.1.1/24` or /29 gateway is left exactly as found. `temp-address`/`temp-dhcp`/`temp-nat` refuse a public prefix, a prefix outside /24../29, a gateway or pool outside the served subnet, and a partial dstnat. A relay's forward accept is scoped to one host and port and removed by `temp-cleanup` with the rest; `temp-ssh` has no cleanup step by design.

Call shape: `routeros-write { op, host, deviceId, interfaze, reason, execute }`. A call is a dry-run plan unless `execute: true` is passed. Every apply ends with a read-back whose output is returned; confirm the resulting state from it. Every successful and blocked attempt lands in the audit log with the caller-supplied `reason`.

## Gates (all must hold or the tool refuses)

```bash
# .env (operator-set; all default OFF/refusing)
NOC_WRITE_ENABLED=true                        # master kill switch
NOC_WRITE_ROUTEROS_TRANSPORT=verified         # one-time ack after lab/spare-router validation
# NOC_WRITE_ALLOWED_HOSTS=<exact IPs and/or CIDRs>  # OPTIONAL. Unset = private/link-local/loopback;
#                                                  # set = replaces that default (narrow, or add a public mgmt IP)
NOC_WRITE_POE_CYCLE_PAUSE_MS=10000            # power-off dwell for poe-cycle (integer 1000..60000)
NOC_WRITE_POE_CYCLE_STRATEGY=forced-on        # or auto, if the port was originally auto
```

Also required: `execute: true`, and a single encrypted exact-device `mikrotik-ssh` credential binding matching the passed `deviceId` + `host` (type-wide credentials are never used for writes). A missed gate is the reason to stop: name it to the operator.

`interface-disable` is refused for names that look like uplink or production ports; that op goes through the operator-led path.

## PoE power-cycle procedure

Use when a radio's ethernet interface is physically up but the radio is unresponsive or unpingable.

1. Confirm port state and the hung radio with the read path (`interface-ethernet-monitor`, `ping` via `run-diagnostics`).
2. Verify the port beyond doubt: require BOTH the interface comment (for example `;;; 5s`) AND the responder's MAC in `/interface bridge host print` on that port. Cycling the wrong port drops a live sector or backhaul.
3. Dry-run: `routeros-write { op: "poe-cycle", host, deviceId, interfaze, reason, execute: false }` and review the exact commands.
4. Apply: same call with `execute: true`. The tool powers the port off, dwells `NOC_WRITE_POE_CYCLE_PAUSE_MS`, restores power, then runs the read-back.
5. Ping-verify the radio came back (`run-diagnostics ping`, 3 times). Do not mark resolved until it answers.

Never enable switch PoE on a port that may also have an external injector feeding the radio (voltage conflict can damage passive-24V devices). If PoE is off on that port by design, ask the tower host instead.

## Bootloader-stuck radio: pings OK but ALL TCP refused

A factory-reset airMAX radio serves SSH (22) and web (80) at `192.168.1.20` immediately. If it answers ping/ARP but every TCP connect times out, even from the gateway or sector switch on the same L2, check these corroborating signs:

- ICMP replies carry TTL 255 with about 0 ms RTT (the bootloader answers; airOS userspace never starts).
- The ethernet link comes up at 100M instead of 1G (bootloader PHY speed; healthy airOS renegotiates gigabit).

The radio is stuck in its bootloader or crash-looping. Do ONE remote PoE power-cycle before giving up:

1. Verify the port (step 2 above).
2. Cycle it with `routeros-write` `poe-cycle` (preferred). Only if the switch has no exact-device binding or the gates are off, the operator may authorize the raw-SSH exception: `/interface ethernet poe set <port> poe-out=off`, wait about 10 seconds, then `poe-out=auto-on` (use `forced-on` if the port was `forced-on`).
3. Watch for link-up (60-120 s), then re-test TCP: SSH banner grab via the temporary dst-nat, or from the switch itself: `/tool fetch url="http://192.168.1.20/" keep-result=no`.
4. Unchanged after a clean cycle: probe TFTP recovery (`curl -m 7 tftp://<relay>/probe.bin` through a temporary UDP dst-nat to port 69). No response either: the radio is dead beyond remote repair (suspect surge damage or a failed unit). Clean up every temporary change (`SKILL.md` Step 5b) and report failure; it needs a field tech.
