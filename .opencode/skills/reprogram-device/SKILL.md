---
name: reprogram-device
description: Use when a network device has factory reset (often after weather, power, or surge events) and needs its backup configuration restored, e.g. "Reprogram beta5n 10.0.40.5", "Reprogram Quebec2GammaST 10.0.41.3". Covers retrieving the backup config from Oxidized or UISP, locating where the reset device is plugged in (gateway router, sector switch, or backhaul switch), and reinstalling the config on UBNT airMAX/airOS AND Wave/MLO devices.
---

# Device Reprogramming (Factory-Reset Restore)

Restore a factory-reset Ubiquiti device to its previous working state using its backup config. The user MUST supply the device IP — the device name is optional context. Example invocations:
- Customer AP: `Reprogram beta5n 10.0.40.5`
- Backhaul AP: `Reprogram Gamma2QuebecAP 10.0.41.2`
- Backhaul ST (router must be supplied): `Reprogram Quebec2GammaST: 10.0.41.3 router: 10.0.41.1`

**Validated live** on a LiteAP GPS (airOS 8, bridge-mode customer AP), a Wave Pro backhaul ST restored via `ucfg backup restore` (`quebec2gammaST`, 10.0.41.3, plugged into a CRS305 switch), and a Wave Pro backhaul ST restored via the factory wizard's **Upload Configuration** option (`nu2psi ST`, 10.0.43.3).

## When to Use

- Device factory reset after weather/power events and needs its config back
- Device is unresponsive at its normal management IP but its PoE/link is up
- User asks to "reprogram", "reconfigure", or "restore" a device and supplies an IP
- Backhaul radio (airMAX or Wave/MLO) factory reset — see Backhaul Radios below
- Wave/MLO radio factory reset — see Wave/MLO Radios below

## When NOT to Use

- No IP supplied — always ask for it first (the skill cannot work without it)
- MikroTik RouterOS devices (need `netinstall` / `.npk` restore — out of scope)
- Device is reachable and working normally (nothing to restore)
- **Backhaul ST side with no router supplied** — you cannot reach the reset radio without knowing which router it plugs into (see Backhaul Radios). Ask for the router IP.

## Access rules: MCP first, operator-led raw SSH as the exception

Restoring a reset device needs router CLI work that no typed MCP tool covers (temporary address, NAT and DHCP entries, switch port checks). Rules:

- Prefer typed tools. Reads: `run-diagnostics` (`ip-address`, `ip-arp`, `interface-list`, `ping`, ...). PoE and interface enable/disable: `routeros-write` (gates and procedure in `references/routeros-write-poe.md`).
- Raw SSH from the agent is an operator-led exception, allowed ONLY for the temporary-access steps in this skill, one reviewed step at a time, with the operator present. The operator exports the device admin password in an environment variable in the agent's shell (`export SSHPASS=...`, typed by the operator, never pasted into chat or onto a command line). Use `sshpass -e`, never `sshpass -p`. Never read `credentials.json`, `.env` or the encrypted store to find it, and never write it to a file, log, comment or ticket. If `SSHPASS` is unset, ask the operator; do not look elsewhere.
- Define once per session (router and switch hops):

```bash
MT_SSH='sshpass -e ssh -o StrictHostKeyChecking=accept-new -o PreferredAuthentications=password -o PubkeyAuthentication=no -o ConnectTimeout=8'
$MT_SSH admin@<gateway> '<RouterOS command>'
```

- A factory-reset radio presents a new host key and speaks legacy crypto, so radio hops use the `LEGACY` options in Step 4 with `StrictHostKeyChecking=no` and the vendor factory default (`SSHPASS=ubnt`, public, not a secret) set per command. That relaxation applies only to the radio on its temporary link-local path, never to routers or switches.
- Tag every temporary RouterOS entry `comment="temp reprogram-device"`, read back after each change, and clean up in Step 5.

## Workflow Overview

1. **Retrieve backup config** — Oxidized first, UISP fallback. For Waves, also pull the
   DEVICE's own `ucfg backup create` tar — Oxidized has no `board.json`, so you need the
   device's to build a restorable file (see `references/wave-ucfg.md`).
2. **Detect device type** (airOS vs Wave) + **determine access router** — from config format, device name, and role
3. **Locate where the device is plugged in** — direct router port, sector switch, or backhaul switch (neighbor print + interface comments)
4. **Prepare access** — airOS: temp `192.168.1.1/24` + NAT. Wave: temp `192.168.1.1/24` + `waveDHCP` DHCP server on the interface facing the device.
5. **Install config** — airOS: SCP `/tmp/system.cfg` + `rc.softrestart save`. Wave: SCP JSON + `ucfg backup restore` over SSH — **preferred**; the factory wizard is a fallback and adds a firmware-compatibility gate (see `references/wave-ucfg.md`)
6. **Verify & clean up** — confirm original IP/SSID, remove ALL temp rules

---

## Step 1: Retrieve the Backup Config

### 1a. Oxidized (primary — works for all UBNT airOS devices)

```bash
node scripts/oxidized_fetch.mjs <device_ip> --out /tmp/reprogram.cfg   # preferred: base URL from OXIDIZED_BASE_URL, prints only identity lines
# fallback: curl -s "$OXIDIZED_BASE_URL/node/fetch/<device_ip>" > /tmp/reprogram.cfg
```

- Nodes are keyed by IP. `GET /nodes.json` lists all nodes (slow, large — avoid unless needed).
- The returned config is ready to install as-is (`# board.*` lines are harmless comments).
- **Wave/MLO devices return a JSON config** (ucfg format — keys like `network.interfaces.data.ipv4`, `system.hostname`, `wireless.radios`, `"version": N`). Do NOT strip anything; the whole JSON is what gets pushed back (see Wave/MLO Radios).
- If the file is empty or the endpoint 404s → try UISP.

### 1b. UISP (fallback)

Auth: header `x-auth-token` carrying the UISP API token. No MCP tool covers backups, so the operator exports the token as `UISP_TOKEN` and you pass it through a curl config on stdin so it never appears on a command line: `printf 'header = "x-auth-token: %s"\n' "$UISP_TOKEN" | curl -sS -K - '<url>'`. Never read it from `credentials.json`. Base path is `/nms/api/v2.1/`.

1. **Find the device.** `GET /devices` returns the FULL inventory (~36k devices, ~90MB) and accepts NO query filters — stream it and filter client-side by MAC or IP:

```bash
printf 'header = "x-auth-token: %s"\n' "$UISP_TOKEN" | curl -sS -K - 'https://uisp.example.com/nms/api/v2.1/devices' | python3 -c "
import sys, json
data, _ = json.JSONDecoder().raw_decode(sys.stdin.read())
ip = '<device_ip>'
for d in data:
    ident = d.get('identification') or {}
    if (ident.get('mac') or '').lower() == '<device_mac>' or d.get('ipAddress','').startswith(ip):
        print(ident['id'], ident.get('name'), ident.get('mac'), d.get('ipAddress'), ident.get('modelName'))
        break"
```

If you don't know the MAC, the device IP in `ipAddress` also works (shown in the filter above).

2. **List backups** — pick the most recent with `"type": "cfg"`:

```bash
printf 'header = "x-auth-token: %s"\n' "$UISP_TOKEN" | curl -sS -K - "https://uisp.example.com/nms/api/v2.1/devices/<device_id>/backups"
```

3. **Download the cfg** and strip the UISP header lines (anything starting with `##`):

```bash
printf 'header = "x-auth-token: %s"\n' "$UISP_TOKEN" | curl -sS -K - "https://uisp.example.com/nms/api/v2.1/devices/<device_id>/backups/<backup_id>" \
  | grep -v '^##' > /tmp/reprogram.cfg
```

The `### PART ###` / `## <timestamp>` / `##WA.<ver>` headers are NOT valid config lines and must be stripped.

### 1c. If neither source has a config

Respond with: **`unable to retrieve device config`** — do NOT proceed.

---

## Step 2: Detect Device Type + Determine the Access Router

### 2a. Detect the device type from the config format

| Config signature | Device type | Install method |
|------------------|-------------|----------------|
| `netconf.N.ip=`, `route.1.gateway=`, `aaa.1.ssid=` | UBNT airOS (airMAX) | SCP `/tmp/system.cfg` + `rc.softrestart save` (Step 4a–4f) |
| JSON: `network.interfaces.data.ipv4`, `system.hostname`, `wireless.radios`, `"version": N`, `eth0@0` | UBNT Wave / MLO | SCP JSON + `ucfg backup restore` (Wave/MLO Radios below) |

Read the retrieved config to classify it:

```bash
grep -E '^netmode=|^netconf\.[0-9]+\.(ip|netmask|devname)=|^route\.1\.gateway=|^aaa\.1\.ssid=' /tmp/reprogram.cfg | head
# Wave JSON instead shows: "cidr": "10.0.41.3/29", "defaultGateway": "10.0.41.1"
```

### 2b. Determine the access router + device role

**Customer AP** (5n, 5s, 5e, 5w, 5x naming — has `aaa.1.ssid=` and a `/24` netmask):
- Gateway = `route.1.gateway` from the config. If absent, take the device IP and change the last octet to `.1`.
- SSH to the gateway under the "Access rules" procedure above (the operator supplies the router admin password in `SSHPASS`):

```bash
$MT_SSH admin@<gateway_ip> '/system identity print'
```

**Backhaul radio** — named `<siteA>` + `2` + `<siteB>` + `AP` (AP side) or `ST` (ST side), e.g. `gamma2quebecAP`, `quebec2gammaST` (actual site names, not ticket abbreviations):

- **AP side:** the radio plugs into the feeding MikroTik that carries the link's `/29` subnet. The gateway is the lowest usable IP in the `/29` (network+1). Prefer the config's gateway when present (handles `/24` bridged exceptions): for airOS read `route.1.gateway`; for Wave read `network.interfaces.data.ipv4.defaultGateway`. Otherwise compute the `/29` from the device IP and confirm that router owns the gateway IP:
  ```bash
  # /29 from a device IP: subnet = IP & 0xFFFFFFF8, gateway = subnet + 1
  # e.g. 10.0.41.2/29 → network 10.0.41.0, gateway 10.0.41.1
  ```
  Verify on the candidate router: `/ip address print` shows the gateway IP on an interface.

- **ST side:** the `/29` gateway is NOT a reliable way to find the router — on ST routers that gateway IP is usually **disabled** (enabled only temporarily for troubleshooting). **You must be told the router.** Expected invocation: `Reprogram quebec2gammaST: <ip> router: <router_ip>` (the radio name, then its IP, then the feeding router's IP). If it's missing, ask (non-interactive: fail).

### 2c. Sector switch vs. main gateway router

Some APs plug into a **sector switch**, not the main gateway router. Identify the sector switch and log into it directly — do NOT assume the config's `route.1.gateway` is where the AP hangs.

How to find the sector switch:
1. SSH to the config's gateway (from Step 2b) and check its neighbors + PPPoE sessions: `/ip neighbor print`, `/ppp active print`.
2. Look for a device named like the site name followed by `sectorswitch` (e.g., `sigmasectorswitch`) as a PPPoE client. Its `ADDRESS` in `/ppp active print` is its PPPoE session IP (e.g., `10.0.44.5`) — SSH to that with the MikroTik creds.
3. The sector switch's ports are commented with the attached APs (`;;; 5n`, `;;; 5s`, ...). Confirm the target AP's port and its state. Disabled ports (e.g., `X` flag) mean that AP is currently powered off — enable it when ready to work on that device:
   ```bash
   $MT_SSH admin@<sector_switch> '/interface enable <port>'
   ```
4. The sector switch may already carry a `192.168.1.1/24` on its bridge (a temp address left from prior reprogram jobs) — reuse it rather than adding another.

**If the sector switch is not directly reachable:**
- It may have no PPPoE session / routable IP of its own. Work **from the router** (the gateway it connects through), or
- Use MikroTik **mac-telnet** from the router to log into the sector switch by MAC: `/tool mac-telnet <sector_switch_mac>` (mac-telnet users/passwords are configured on the switch itself). Note: the MCP server may not currently expose mac-telnet — do this via direct SSH to the router.

### 2d. Backhaul radios can hang off a switch (not the router directly)

Backhaul radios — especially the "bridged/switched" parts of the network — often plug into a **CRS switch**, not directly into the access router. The access router connects to that switch on one interface. To find where the reset device is plugged in:

1. On the access router, `/ip neighbor print` and `/interface print detail where comment!=""`.
2. Find the interface **facing the switch** (the uplink). Its comment names the switch (e.g., `;;; crs 305`).
3. SSH to the switch and check `/interface print detail where comment!=""` + `/ip neighbor print`. Port comments name the attached backhauls (e.g., `;;; gamma 60Pro`).
4. **Factory-reset signature:** a reset device appears in `/ip neighbor print` as a neighbor with a MAC address but **NO IP** (it's sitting at `192.168.1.20`). Look for the MAC that matches the target device on the commented port.
5. **DHCP server + pings run from the access router, on the interface facing the switch** — the switch is transparent L2, so broadcasts and pings reach the reset device through it. You do NOT need to configure the switch itself.

**If you cannot determine or reach the gateway/sector switch:**
- Interactive mode → ask the user which router to access the device from.
- Non-interactive → return failure (cannot proceed without a gateway).

---

## Step 3: Prepare Access on the Gateway Router

### 3a. Find the interface the device is on

The router's interface comments name the attached APs (`;;; 5n`, `;;; 5s`, ...):

```bash
$MT_SSH admin@<gateway> '/interface print detail where comment!=""'
```

Find the comment matching the device name (e.g., `5n` → `ether2`).

### 3b. Determine the L3 interface

If the port is a bridge member (common — APs are bridged), the IP must go on the **bridge**, not the port:

```bash
$MT_SSH admin@<gateway> '/interface bridge port print'
```

If the device's port is in the bridge → use the bridge name (e.g., `bridge1`). Otherwise use the port name directly.

### 3c. Add the temp access path

A factory-reset airOS device sits at `192.168.1.20/24` with NO default route. You need three temp changes (all tagged `comment="temp reprogram-device"` so they're identifiable for cleanup):

```bash
# 1. Put the router on the reset device's subnet (use the L3 interface from 3b)
$MT_SSH admin@<gateway> \
  '/ip address add address=192.168.1.1/24 interface=<bridge_or_port> comment="temp reprogram-device"'

# 2. Masquerade traffic to 192.168.1.0/24 — REQUIRED so the AP's replies reach
#    the router (the AP has no default route and won't answer off-subnet sources)
$MT_SSH admin@<gateway> \
  '/ip firewall nat add chain=srcnat action=masquerade dst-address=192.168.1.0/24 out-interface=<bridge_or_port> comment="temp reprogram-device"'

# 3. dst-nat to a routable router IP. NOTE: many gateways have NO ssh/telnet client,
#    and the agent host may not be able to route to 192.168.1.0/24 directly, so NAT
#    a reachable router address to the AP. The router's loopback IP is ideal.
#    (Find it: /ip address print where interface=loopback)
$MT_SSH admin@<gateway> \
  '/ip firewall nat add chain=dstnat action=dst-nat protocol=tcp dst-address=<router_loopback_or_mgmt_ip> dst-port=2222 to-addresses=192.168.1.20 to-ports=22 comment="temp reprogram-device"'
```

> **Alternative to step 3c/3:** if the agent host is directly on the routed backbone, add a host route and SSH straight to 192.168.1.20 — `ip route add 192.168.1.0/24 via <gateway>`. If `ip route` errors with `Nexthop has invalid gateway`, the gateway isn't on-link for you → use the dst-nat method instead (it always works).

### 3d. Confirm the reset device is there

```bash
$MT_SSH admin@<gateway> '/ping 192.168.1.20 count=3'
```

If it doesn't answer: the device may be powered off, PoE-failed, or mid-boot — report and stop (do not leave temp rules behind — clean up first, see Step 5).

> **Proxy-ARP trap:** gateways/sector switches with `arp=proxy-arp` (common on this network) answer ARP for ANY routable IP, so a successful ARP/ping does NOT prove the reset device sits on that port — another `192.168.1.20` elsewhere in the routed network can answer through proxy-ARP + default routes. Confirm placement with `/interface bridge host print` (FDB) for the responder's MAC, or sniff the port while pinging.

### 3d-2. Bootloader-stuck signature: pings OK but ALL TCP refused

If the radio answers ping/ARP but every TCP connect times out, with TTL 255 replies and a 100M link, it is stuck in its bootloader. Do ONE verified remote PoE power-cycle, then a TFTP probe, before reporting a field-repair failure. Procedure, port verification and PoE safety: `references/routeros-write-poe.md` ("Bootloader-stuck radio"). Prefer `routeros-write` `poe-cycle` over raw SSH.

### 3e. Wave/MLO devices: set up the waveDHCP server instead of NAT

Wave/MLO radios **ping on `192.168.1.20` but have NO SSH by default in factory mode**. They must grab a DHCP lease before they become manageable. Do NOT add the airOS NAT rules for waves — add the DHCP server instead:

1. Ensure `192.168.1.1/24` exists on the interface facing the device (there is often a disabled leftover from a prior job — reuse/enable it rather than adding a duplicate):
   ```bash
   $MT_SSH admin@<gateway> \
     '/ip address print where address~"192.168.1."'
   # If a disabled one exists on the right interface, enable it:
   #   /ip address enable [find where address="192.168.1.1/24"]
   # Otherwise add one (tag it "temp reprogram-device"):
   #   /ip address add address=192.168.1.1/24 interface=<iface> comment="temp reprogram-device"
   ```

2. Create the DHCP server (all tagged `comment="temp reprogram-device"` for cleanup). **The single-IP pool at `192.168.1.100` is the standard**:
   ```bash
   $MT_SSH admin@<gateway> \
     '/ip pool add name=waveDHCP ranges=192.168.1.100 comment="temp reprogram-device"'
   $MT_SSH admin@<gateway> \
     '/ip dhcp-server add address-pool=waveDHCP interface=<iface_facing_device> lease-time=5m name=waveDHCP comment="temp reprogram-device"'
   $MT_SSH admin@<gateway> \
     '/ip dhcp-server network add address=192.168.1.0/24 gateway=192.168.1.1 comment="temp reprogram-device"'
   ```
   If `waveDHCP` or the network entry already exists from a prior job, reuse them (`/ip pool print`, `/ip dhcp-server print`, `/ip dhcp-server network print` to check).

3. Wait for the wave to grab a lease (can take a while). Poll leases:
   ```bash
   $MT_SSH admin@<gateway> '/ip dhcp-server lease print'
   ```
   Look for a lease on `192.168.1.100`. Give it up to ~5 minutes.

4. **If it never grabs a lease (≤5 min):** either
   - the device is stuck in **TFTP recovery mode** (a known bug — it will never lease), or
   - it needs a **power cycle** — ask the tower host of the site to power-cycle / reboot the radio, then re-poll for a lease.
   Respond with the appropriate error (stuck-in-TFTP vs needs-power-cycle) and clean up the temp rules (Step 5). Do not leave the DHCP server running indefinitely.

### 3e-2. Multiple waves at once: one DHCP server PER radio, distinct subnet each

Two or more factory-reset waves on one gateway each need their own subnet (`192.168.N.0/24`) and pool/server name (`waveDHCP`, `waveDHCP2`, ...). Never stack them on one `192.168.1.0/24`: they all self-assign `192.168.1.20` and collide. Poll leases per server with the regex form `where server~"waveDHCP"` (the exact form `=` silently skips `waveDHCP2` and up). Full allocation table, commands and sequencing: `references/multi-wave-dhcp.md`.

### 3f. Wave/MLO: restore via SSH + `ucfg backup restore` (PREFERRED)

The wizard is a firmware-independent bootstrap, but NOT a firmware-independent restore. Creating the local account and enabling SSH in the wizard carry no firmware dependency; only the wizard's **Upload Configuration** step enforces a firmware-compatibility gate. So the default flow is: wizard (account + SSH) then push the config over SSH (Step 4g). Both halves are firmware-agnostic, so this restores cleanly across a firmware mismatch.

Validated on a Wave-LR on firmware 4.1.0 with a 4.1.1 backup: the wizard's upload rejected the file with *"Incompatible backup configuration file"* on every structural variant tried, while `ucfg backup restore` over SSH applied it. Do NOT conclude "firmware must match" from a wizard rejection alone, and do not read its *"from Wave-LR"* wording as a model mismatch when `board.json` already confirms the model.

Bootstrap order: a factory-reset wave has no SSH until it grabs a DHCP lease (Step 3e), and SSH stays off until enabled in the device UI after the local account exists. That is expected, not a fault.

The wizard's Upload Configuration is only a fallback for when SSH cannot be enabled at all. Full flow (build `restore.ucfg`, relay HTTPS to the wave, drive the wizard, verify): `references/wave-ucfg.md` ("Wave factory wizard"). After the reboot, verify per Step 5a.

---

## Step 4: Install the Config (airOS)

The AP's SSH server is old dropbear offering only `ssh-rsa`/`ssh-dss`. **You MUST pass the legacy crypto flags** or the handshake fails:

```bash
LEGACY="-o StrictHostKeyChecking=no -o HostKeyAlgorithms=+ssh-rsa,ssh-dss -o PubkeyAcceptedAlgorithms=+ssh-rsa -o PreferredAuthentications=password -o PubkeyAuthentication=no"
```

**4a. Log in with the factory default creds** (`ubnt` / `ubnt`):

```bash
timeout 15 env SSHPASS=ubnt sshpass -e ssh $LEGACY -p 2222 ubnt@<router_loopback_or_mgmt_ip> 'cat /proc/ubnt_model'
```

Verify you're on the right device (compare the MAC/model with the backup config).

**4b. Back up the factory config** (so the reset state can be restored if needed):

```bash
SSHPASS=ubnt sshpass -e ssh $LEGACY -p 2222 ubnt@<router_loopback_or_mgmt_ip> 'cp /tmp/system.cfg /tmp/system.cfg.default'
```

**4c. Transfer the backup config** — SCP (NOT shell-echo): the config contains `$` password hashes that would be mangled by any shell interpolation:

```bash
SSHPASS=ubnt sshpass -e scp -O $LEGACY -P 2222 /tmp/reprogram.cfg ubnt@<router_loopback_or_mgmt_ip>:/tmp/system.cfg
```

**4d. Verify the transfer landed** (line count + key settings):

```bash
SSHPASS=ubnt sshpass -e ssh $LEGACY -p 2222 ubnt@<router_loopback_or_mgmt_ip> 'wc -l /tmp/system.cfg; grep -E "^netconf\.[0-9]+\.ip=|^aaa\.1\.ssid=" /tmp/system.cfg'
```

**4e. Activate** — applies the config and reboots the device onto its original IP/SSID. The SSH session will drop mid-command (expected):

```bash
SSHPASS=ubnt sshpass -e ssh $LEGACY -p 2222 ubnt@<router_loopback_or_mgmt_ip> '/usr/etc/rc.d/rc.softrestart save'
```

**4f. Wait for it to come back** (~20-40s), then confirm it answers on its original IP:

```bash
for i in $(seq 1 18); do ping -c 1 -W 2 <original_ip> >/dev/null 2>&1 && { echo "UP"; break; }; sleep 5; done
```

### 4g. Wave/MLO devices: install via `ucfg` (non-interactive)

Once the wave has a lease (Step 3e) it is reachable at its leased IP (`192.168.1.100`). If SSH is off, bootstrap it via the wizard first (create local account → enable SSH) — both steps are firmware-independent. The interactive method is `ucfg edit` (opens vi, paste the backup JSON, `:wq` — config auto-saves). **As an agent, use the non-interactive `ucfg backup restore` path instead:**

1. SSH to the wave on its leased IP with the factory `ubnt`/`ubnt` creds (see `references/wave-ucfg.md` for the SSH flags — waves use modern OpenSSH, no legacy crypto needed):
   ```bash
   SSHPASS=ubnt sshpass -e ssh -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no ubnt@<leased_ip> 'ucfg -h'
   ```
2. Check the config version matches the backup (the backup must be at the same `"version": N` or it won't take):
   ```bash
   SSHPASS=ubnt sshpass -e ssh ... ubnt@<leased_ip> 'ucfg show 2>/dev/null | grep -m1 version'
   ```
   If the backup version differs, migrate it first on the device (see `references/wave-ucfg.md`).
3. SCP the backup JSON up, then restore it (skip validation + reload applies it live):
   ```bash
   SSHPASS=ubnt sshpass -e scp -O -o StrictHostKeyChecking=no /tmp/reprogram.cfg ubnt@<leased_ip>:/tmp/reprogram.cfg
   SSHPASS=ubnt sshpass -e ssh ... ubnt@<leased_ip> 'ucfg backup restore -N /tmp/reprogram.cfg'
   ```
   Fallback if `backup restore` is unavailable: `ucfg reload /tmp/reprogram.cfg -c` (or `ucfg commit /tmp/reprogram.cfg`).
4. The device applies the config and reboots onto its original IP. Confirm it answers on the original IP (same loop as 4f).

> **`backup restore` needs a tar, and `migrate` cannot down-migrate.** The bare-JSON path
> above fails with `tar: invalid magic` unless you build a `config.json`+`board.json` tar —
> use `references/build_restore.py`. If the backup's version is *higher* than the device's,
> `ucfg migrate` fails with `failed to achieve required target version`; instead keep the
> DEVICE's version in the merged file. Always `ucfg backup validate` before applying.

---

## Step 5: Verify & Clean Up

**5a. Deep verification** — read the device back on its original IP. Preferred: the MCP `run-diagnostics` `ubnt-airos` reads (`status`, `interfaces`, `board-info`), which use the encrypted store. If you need the on-device config, the backup holds only password hashes, so the operator exports the restored device password as `SSHPASS` (same rules as "Access rules") and you run:

```bash
sshpass -e ssh $LEGACY ubnt@<original_ip> \
  'grep -E "^netconf\.[0-9]+\.ip=|^aaa\.1\.ssid=" /tmp/system.cfg; iwconfig 2>/dev/null | grep ESSID'
```

The original management IP must be live on `br0` and the SSID must be broadcasting.

**5b. Clean up ALL temp changes** — this is REQUIRED; do not leave the network modified:

```bash
# Remove temp NAT rules + address on the gateway (identified by comment)
$MT_SSH admin@<gateway> \
  '/ip firewall nat remove [find where comment="temp reprogram-device"]; /ip address remove [find where comment="temp reprogram-device"]'

# Wave devices: also remove the temp DHCP server, network, and pool (identified by comment).
# This removes EVERY temp entry at once, so it covers multi-wave jobs (waveDHCP, waveDHCP2,
# waveDHCP3, ...) in a single pass — no need to enumerate names.
$MT_SSH admin@<gateway> \
  '/ip dhcp-server remove [find where comment="temp reprogram-device"]; /ip dhcp-server network remove [find where comment="temp reprogram-device"]; /ip pool remove [find where comment="temp reprogram-device"]'

# Verify nothing temp survives — every one of these should print empty:
$MT_SSH admin@<gateway> \
  '/ip dhcp-server print detail where comment="temp reprogram-device"; /ip dhcp-server network print where comment="temp reprogram-device"; /ip pool print where comment="temp reprogram-device"; /ip address print where comment="temp reprogram-device"; /ip firewall nat print where comment="temp reprogram-device"'

# If you added a host route on the agent box, remove it too
ip route del 192.168.1.0/24 via <gateway> 2>/dev/null || true

# Confirm the device is still up after cleanup
ping -c 2 -W 2 <original_ip>
```

> If you REUSED a pre-existing `192.168.1.1/24` (or existing `waveDHCP` pool/server) that you enabled rather than created, disable it back to its prior state rather than removing it — only remove what you created.

> **Also disable leftover port-forwards to factory-default IPs.** A dst-nat pointing at
> `192.168.1.20` (or any `192.168.1.x` factory address) is almost always debris from an
> earlier job and should not be left enabled. Check `/ip firewall nat print`, and disable
> such a rule before starting if it could collide with the temp subnet you are about to
> create, and disable it again during cleanup if you did not create it.

---

## Device-Type Config Install Methods

| Device | Method | Status |
|--------|--------|--------|
| UBNT airMAX / airOS (5.x–8.x) | SCP to `/tmp/system.cfg` + `/usr/etc/rc.d/rc.softrestart save` | ✅ Tested live |
| UBNT Wave / MLO (airOS 9 / ucfg JSON) | SCP JSON + `ucfg backup restore -N` over SSH (preferred — no firmware gate; bootstrap SSH via wizard) — wizard **Upload Configuration** only as fallback | ✅ Tested live (both) |
| MikroTik RouterOS | `netinstall` / `.npk` restore | ❌ Out of scope |

## Error Handling

| Situation | Action |
|-----------|--------|
| No config in Oxidized or UISP | Respond `unable to retrieve device config`, stop |
| `192.168.1.20` won't answer ping | Device off / PoE / mid-boot — clean up temp rules, report |
| Radio answers ping but ALL TCP times out (TTL 255 replies, 100M link) | Bootloader-stuck / crash-loop — ONE verified PoE power-cycle (Step 3d-2); unchanged → TFTP probe → still silent: clean up, report field-repair failure |
| SSH handshake fails with `no matching host key type` | Missing the legacy crypto flags (see Step 4) |
| `ubnt/ubnt` login fails | Ask the operator for the device password (exported as `SSHPASS`); if none works, the device password was changed pre-reset or web-UI/tech is needed |
| `ip route` on agent errors `Nexthop has invalid gateway` | Gateway not on-link → use the dst-nat method (Step 3c) |
| Gateway router unreachable / unknown | Interactive: ask user. Non-interactive: fail |
| Device doesn't come back on original IP after ~90s | Re-ping, check PoE/link on the router, clean up temp rules, report |
| Wave never grabs a DHCP lease (≤5 min) | Stuck in TFTP recovery mode (bug — won't lease) OR needs a power cycle — ask the tower host to reboot the radio, then re-poll; clean up temp rules |
| Wave SSH connection refused in factory mode | Normal — the wave has no SSH until it grabs a lease (Step 3e), and SSH stays off until enabled in the UI after the local account is created (Step 3f). Bootstrap it; do not brute-force |
| Backhaul ST with no router supplied | Ask for the router IP (cannot reach the reset radio without it) |
| Reset device shows in neighbor print with MAC but no IP | Expected factory-reset signature — that's the device on the switch; proceed (Step 2d) |
| Wave `ucfg backup restore` rejects config (version mismatch) | Migrate the backup to the device's current version with `ucfg migrate` (see `references/wave-ucfg.md`) — but note `migrate` only works *upward*; to restore a higher-version backup onto an older device, keep the DEVICE's `version` in the merged `config.json` instead |
| Wave `backup restore` fails with `tar: invalid magic` | It was handed bare JSON. `backup restore`/`migrate` require a gzipped tar of `config.json` + `board.json` — build one with `references/build_restore.py` |
| Wave refuses a correct password repeatedly (`Permission denied`) | Rate-limiting on rapid SSH logins. Retry 3–4 times with ~4s gaps; do **not** re-check credentials or re-lease |
| Restored wave answers no ping from the router | Test with `src-address=<radio defaultGateway>` from the backup config. A router may not answer at all from its default source while the radio is healthy; trust OSPF `Full` + an external ping |
| Restored wave `iwconfig` shows `Not-Associated` / `Link Quality=0` | That is the idle 5 GHz **backup** radio. Check the 60 GHz interface carrier, and use OSPF `Full` as the real backhaul signal |
| Wave wizard rejects a valid backup as *"Incompatible backup configuration file"* | Expected when device firmware ≠ backup firmware — the wizard's upload gates on firmware, `ucfg` does not. Bootstrap via wizard (account + enable SSH), then push the config over SSH (Step 4g) instead of flashing. Do not repack the tar; the *"from Wave-LR"* wording is not a model mismatch |
