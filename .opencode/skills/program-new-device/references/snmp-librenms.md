# SNMP will not poll — triage runbook

Ordered by how often it is the cause in this network. Work it top to bottom and
stop at the first thing that is wrong. Placeholders only: the poller list and
community come from configuration (`NOC_SNMP_COMMUNITY`,
`NOC_SNMP_COMMUNITY_ADDRESSES`), never from this file.

Symptom either way: LibreNMS says `Could not connect` when adding, or an
existing device turns red with `Device status changed to Down from snmp check`.

## 1. RouterOS `src-address` (silent killer)

`/snmp print` shows `enabled: yes` and the device still answers nothing. Look at
`src-address:` — when it holds an address the device no longer owns (stale
management IP after a renumber, or the old loopback), the daemon binds to an
address that is not there and stops answering queries. Nothing logs this.

```
/snmp print                     # src-address: 10.x.x.x  <- wrong
/snmp set src-address=::        # any local address
```

`::` is the only value that survives a renumber. `snmp-config snmp-enable`
resets a configured src-address to `::` on its own; `snmp-verify` reports it.

## 2. Poller allow list on the community

The community must include the sources LibreNMS polls from — the poller's own
management subnets plus any address it reaches the device *from* (a router
transit path can make it arrive with a public source; check the traceroute in
step 3 before assuming a private one).

```
/snmp community print detail    # addresses=...
```

`addresses=` empty (or `0.0.0.0/0`) accepts everything; a list missing the
poller's source looks exactly like "SNMP is broken". `snmp-config` writes the
list from `NOC_SNMP_COMMUNITY_ADDRESSES` and leaves it alone when unset.

## 3. Reachability and the path the poller takes

Ping the device from the LibreNMS host, then traceroute it. If the route leaves
through the internet, the device sees the poller as the public source address of
the edge that NATs it, not a private one — that is the address the community
list and the firewall rule must allow.

## 4. SNMP version

Many UBNT/airOS radios and older devices answer **v1 only**; a v2c-only poll
fails with a timeout that looks identical to a wrong community. Set the version
per device in LibreNMS (`v1` for those), not globally.

## 5. Firewall on the router in front of the device

SNMP is UDP **161**. On MikroTik, check the `securemikrotik` allow chain (or the
input/forward rule that allows management traffic) covers the poller sources and
UDP 161. A filter rule that accepts the source but not the port silently drops
the query. Add 161 to the existing UDP allow rule rather than a second rule.

```
/ip firewall filter print where chain~"securemikrotik"
/ip firewall filter print where protocol=udp and dst-port~"161"
```

## 6. Loopback addressing (infrastructure routers, backhauls, PPPoE)

Poll infrastructure routers by **loopback**, not by a transit or customer-facing
address: transit addresses move and go red on renumber. After changing the
address in LibreNMS, also set the router's SNMP source to that loopback.

Two active loopbacks on one router will poll badly — the OSPF/BGP instance uses
one of them. Determine which is active, remove the other from `/ip address` and
`/routing ospf network`.

## 7. Duplicate sysName

`Duplicate sysName` on add means the device's identity/hostname/hostname is not
unique. Set it unique (site name in the identity), then add again.

## 8. Down device with almost no data

Only the IP and the last logs show: the device is offline or locked up. Confirm
power/path first (`reprogram-device` if it is a reset), and identify what the IP
belongs to (The Dude, IP Network sheet, ARP/DHCP on the serving router, traceroute)
before deleting the entry in LibreNMS.

## Confirm it actually polls

LibreNMS: device → three-dot menu → bug icon → **SNMP → Run** under *Capture
Debug Information*. Real data points back = SNMP answers; adding the device then
works. Polling up in the device list is the only real proof — a read-back from
the router is not.
