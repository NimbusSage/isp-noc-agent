# Peering Template — Live Copy + Secure Creds

## Credential Resolution (secure)

Credentials come from the MCP encrypted store (`mikrotik-ssh` pool, priority order; passphrase `NOC_MCP_PASSPHRASE`) — never hard-code them in files, commands that are logged, or comments.

```bash
# Resolve EVERY mikrotik-ssh pool credential once (via the MCP store / operator-supplied env); try them until one authenticates.
# PW_POOL is a bash array populated at runtime from the encrypted store — never from a tracked file.
for pw in "${PW_POOL[@]}"; do SSHPASS="$pw" sshpass -e ssh admin@$RR '...' && break; done
```

## Live Template Fetch

**RR side** (existing peer): `ssh admin@10.0.20.1 '/routing/bgp/connection print detail where remote.address=10.0.200.30'` (xray) →
```
remote.address=10.0.200.30 .as=<REMOTE_AS> local.address=10.0.20.1 .role=ibgp-rr routing-table=main templates=default as=<REMOTE_AS> multihop=yes cisco-vpls-nlri-len-fmt=auto-bits output.network=bgp-networks
```
Copy `remote.as`, `templates`, `multihop`, `output.network`, `cisco-vpls-nlri-len-fmt`, `output.filter-chain` if present, `local.role`.

**Tower side** (existing delta peer): `ssh admin@10.0.200.31 '/routing/bgp/connection print detail where name~delta'` →
```
remote.address=10.0.22.1/32 .as=<REMOTE_AS> local.address=10.0.200.31 .role=ibgp ... output.network=bgp-networks
```
If `instance=bgp-instance-1` present (e.g. `Whiskey 10.0.200.31` on RouterOS `7.22.3`), include it — RouterOS `missing value instance` error otherwise. Detect via `system resource print version` or via `connection print detail` containing `instance=`.

## Add Commands (TEMP prefix)

**RR:**
```bash
SSHPASS="$PW_RR" sshpass -e ssh -o StrictHostKeyChecking=accept-new admin@$RR \
  "/routing/bgp/connection add name=\"TEMP $orig\" remote.address=$loopback remote.as=$REMOTE_AS local.address=$RR local.role=ibgp-rr routing-table=main templates=default multihop=yes output.network=bgp-networks"
```

**Tower:**
```bash
INST=$(ssh admin@$tower '/routing/bgp/connection print detail where name~delta' | grep -o 'instance=[^ ]*' | head -1)
SSHPASS="$PW_TOWER" sshpass -e ssh admin@$tower \
  "/routing/bgp/connection add ${INST:+$INST }name=\"TEMP 2alphabgprr\" remote.address=$RR remote.as=$REMOTE_AS local.address=$loopback local.role=ibgp routing-table=main templates=default multihop=yes output.network=bgp-networks"
```

Use `TEMP 2alphabgprr` / `TEMP 2bravobgprr` etc. per `RR` to match existing `2alphabgprr` naming.

## Handling Disabled/Not-Used

`Oxidized` dump contains `disabled=yes` (`X`) entries like `TEMP 2 OLD-PEER` — **do not copy disabled**. Live check: `ssh admin@$RR '/routing/bgp/connection print where disabled=no'` — only those with `E` established are "actually running redundantly". For tower, `ping $RR 0%` confirms OSPF border running before peering.

Skipped sites earlier (`Echo` manual fixes) were due to RouterOS `missing instance` failure — now handled via `instance` detection + retry `instance=bgp-instance-1`.

## Verify

```bash
ssh admin@$RR '/routing/bgp/session print where remote.address~"<loopback>"' | grep "E -" # prefix-count>0
ssh admin@$RR '/ip route print where dst-address=<subnet>' | grep "DAb <subnet> via <loopback>"
ssh admin@$RR '/ping address=<subnet_ip> count=3' | grep "0% loss"
```

If still loop, re-traceroute same `<subnet>` → next `last_ip`.
