# Wave/MLO Devices: ucfg Config Management Reference

Wave/MLO radios run UISP's airOS 9 and manage config through the `ucfg` CLI (a JSON
config blob). This reference supports the Wave/MLO workflow in `SKILL.md`.

## SSH to a Wave device

Waves run modern OpenSSH — **no legacy crypto flags needed** (unlike airOS dropbear).
Factory-default creds are `ubnt`/`ubnt` (the public vendor default, passed through `SSHPASS` so it never sits on a command line; any other device password comes from the operator the same way, see `SKILL.md` "Access rules").

```bash
SSHPASS=ubnt sshpass -e ssh -o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no ubnt@<device_ip> '<command>'
```

## ucfg command summary

| Command | Purpose |
|---------|---------|
| `ucfg -h` | List available commands on the device (always check this first) |
| `ucfg show` | Print the current config (JSON) |
| `ucfg show <path>` | Print a config from a file path |
| `ucfg edit [-c] [<filter>]` | Interactive edit (vi TUI). **Avoid as an agent.** |
| `ucfg commit [<path>]` | Commit the active config to persistent memory (optionally from a file) |
| `ucfg reload <path> [-c]` | Load a config from a file (with `-c` to commit) |
| `ucfg start <path>` | Start the system using a config file |
| `ucfg backup create` | Create a system backup |
| `ucfg backup validate <path>` | Validate a backup file |
| `ucfg backup restore <path> [-r] [-N]` | Restore from a backup file. `-N` skips validation; `-r` reloads after |
| `ucfg defaults [-r|-R]` | Reset to factory defaults (`-R` reboots) |
| `ucfg migrate <old_path> <target_version> <new_path>` | Migrate a config to a specific version |

## Non-interactive config restore (agent-friendly) — PREFERRED PATH

**This is the primary path for Wave/MLO restores.** It has **no firmware-compatibility
gate**, so a backup taken on newer firmware than the factory image still applies. Reaching
it means bootstrapping through the wizard first (create local account → enable SSH), which
is itself firmware-independent. See "Wave firmware gate" below for the validated case.

### Enabling SSH over the authenticated API (skips manual UI clicking)

Once the local account exists, SSH is still **off** until enabled. Rather than hunting for
the toggle in the UI, read the bearer token from `sessionStorage` after login and enable it
via the API:

1. Log in through the wizard. The token lands in `sessionStorage` as `x-auth-token`
   (a 40-hex string). Note: it is **not** a cookie — `/api/*` returns 401 with cookies alone.
2. API paths require a version prefix: `/api/v1.0/...` (bare `/api/services` returns
   *"Unsupported URL format - it should start with /api/vx.y/"*).
3. Enable SSH:
   ```bash
   curl -sk -X PUT -H "x-auth-token: $TOK" -H "Content-Type: application/json" \
     -d '{"sshServer":{"enabled":true,"sshPort":22,"passwordAuthentication":true}}' \
     "https://<relay_host>:<port>/api/v1.0/services"
   ```
   Verify with `GET /api/v1.0/services` → `sshServer.enabled: true`.

### `ucfg backup restore` takes a TAR, not bare JSON

`ucfg backup restore <path>` and `ucfg migrate <path>` both expect a **gzipped tar** of
`config.json` + `board.json`. Handing them a bare JSON file fails with
`tar: invalid magic`. The wizard's **Upload Configuration** uses the same tar format.

**Oxidized backups do NOT contain `board.json`** — they are `config.json` only (with a
Ubiquiti copyright banner above the JSON; strip everything before the first `{`).
To build a restorable tar you must supply a real `board.json` from the device:

```bash
# 1. ask the device for its own backup — correct schema, real board.json, own tar metadata
ucfg backup create /tmp/dev.ucfg
scp -O ... <leased_ip>:/tmp/dev.ucfg /tmp/dev.ucfg
# 2. deep-merge the Oxidized config.json values into the device's own config.json
#    (backup values win; the device's structural keys are retained), then repack a tar
#    reusing the DEVICE's tar member metadata (uid/gid/uname/gname/mode/mtime)
# 3. always keep the DEVICE's "version" value so ucfg accepts the file
# 4. VALIDATE BEFORE APPLYING — do not skip this
ucfg backup validate /tmp/restore.ucfg   # must exit 0
ucfg backup restore -r /tmp/restore.ucfg
```

Working merge script: `build_restore.py` (see the three-radio validated case below).

### `ucfg migrate` cannot down-migrate

Validated: backup `"version": 47`, device on `45` →
`ucfg migrate <file> 45 <out>` fails with **`failed to achieve required target version`**.
Do not spend time retrying it. Instead keep the **device's** version in the merged
`config.json` (the device validates against its own schema, which the device-generated
`config.json` already satisfies).

The manual method is `ucfg edit` → paste the backup JSON over everything → `:wq`
(ucfg validates the version and auto-saves live). For an agent:

1. SCP the backup JSON to the device:
   ```bash
   SSHPASS=ubnt sshpass -e scp -O -o StrictHostKeyChecking=no /tmp/reprogram.cfg ubnt@<leased_ip>:/tmp/reprogram.cfg
   ```
2. Restore it (skip validation, reload applies it live):
   ```bash
   SSHPASS=ubnt sshpass -e ssh ... ubnt@<leased_ip> 'ucfg backup restore -N /tmp/reprogram.cfg'
   ```
   Fallbacks if `backup restore` is unavailable:
   - `ucfg reload /tmp/reprogram.cfg -c`
   - `ucfg commit /tmp/reprogram.cfg`
   - `ucfg start /tmp/reprogram.cfg` (service restart from the file)

## Version matching

The config version **must match** the device's current ucfg schema or the restore
won't take. Check both:

```bash
# Device's current version
SSHPASS=ubnt sshpass -e ssh ... ubnt@<device> 'ucfg show 2>/dev/null | grep -m1 version'
# Backup's version
grep -m1 '"version"' /tmp/reprogram.cfg
```

If they differ, migrate the backup on the device:

```bash
SSHPASS=ubnt sshpass -e ssh ... ubnt@<device> 'ucfg migrate /tmp/reprogram.cfg <device_version> /tmp/reprogram.migrated.cfg'
# then restore the migrated file
```

## Wave factory wizard: "Upload Configuration" restore (FALLBACK — prefer SSH/ucfg)

A factory-reset Wave/MLO radio's setup wizard has a built-in **Upload Configuration**
option that restores a full backup in one step — **no account creation, no SSH, no
`ucfg` needed.** Validated live on a Wave Pro ST (`nu2psi ST`,
10.0.43.3).

### 1. Build the restore.ucfg

A wave backup is a tar of `config.json` + `board.json` (exactly what a UISP `tar`
backup or the wizard's own download produces). Build it from the backup config +
board:

```bash
mkdir -p /tmp/prep && cd /tmp/prep
# config.json + board.json from the UISP tar backup (or Oxidized JSON + device board)
tar czf /tmp/restore.ucfg config.json board.json
```

No version patching was needed when backup firmware matched the factory firmware
(4.1.1 backup → 4.1.1 factory) — the wizard validated and staged the file as-is.

### 2. Reach the wizard web UI

The factory wizard is a web SPA on HTTPS. With the wave on a DHCP lease
(`192.168.1.100`), relay a router port to its web port and drive it from the agent
with Puppeteer (`/usr/bin/google-chrome`, `--ignore-certificate-errors`). You need
both a dstnat relay **and** a matching srcnat masquerade so replies route back.

### 3. Drive the wizard

1. Select country **United States** (searchable dropdown: click
   `input[placeholder="Select"]`, type it, click the exact option).
2. Check `#agreed` (Terms of Use).
3. Click **Upload Configuration** (enabled only after country + terms).
4. Attach the `restore.ucfg` to the hidden `input#file-upload`.
5. Click **Continue** → device prompts: *"Do you want to apply uploaded
   configuration (the device will reboot)?"* → click **Apply**.
6. Device applies the config and reboots onto its original IP (confirm via ARP —
   MAC match on the device's static subnet).

### 4. After the reboot

- Post-restore SSH uses the backup's `users[0].password` hash — wave-restore default
  password, resolved from the untracked MCP credential pools (never tracked here).
- UISP reconnects automatically (the backup carries the UISP key).
- Verify: original static IP live on `br0`, hostname, wireless link (ST side
  associates to its AP), internet, and UISP `authorized: true`.

### Relay/masquerade gotcha

dstnat relays to the wave's web/SSH work only while a srcnat masquerade covers the
wave's **current** subnet. If the wave moves from `192.168.1.0/24` (DHCP) to its
static `/29`, relays silently stop answering — add
`srcnat masquerade dst-address=<static_subnet> out-interface=<iface>`. The `.1`
gateway address on an ST router is normally DISABLED (enable only during
troubleshooting); the ST's real gateway lives on the AP-side router, reached over
the wireless bridge.

## Wave firmware gate: why SSH/`ucfg` is the restore path (validated on `Alpha 2 Mike ST Wave LR`, Wave-LR, 10.0.42.12)

**The wizard is a firmware-independent bootstrap, not a firmware-independent restore.**
Those are two different halves of it, and only the second one has a firmware gate:

| Wizard step | Firmware gate? | Notes |
|---|---|---|
| Country + terms + **create local account** | **No** | Works on any firmware — this is how you bootstrap |
| Enable SSH (device UI) | **No** | Also firmware-independent |
| **Upload Configuration** (config restore) | **Yes** | Rejects backups whose firmware ≠ device firmware |

So the correct default flow is: **wizard to create the account → enable SSH → push the
config over SSH with `ucfg backup restore`.** Account creation and SSH enablement carry no
firmware dependency, and `ucfg` has no gate either, so this path restores across a
firmware mismatch. There is no need to fall back to the wizard's upload — treat
"Upload Configuration" as a fallback for the rare case where SSH can't be enabled at all.

Validated case: device on **4.1.0**, backup on **4.1.1**. The wizard's upload rejected it
with *"Incompatible backup configuration file. Please upload configuration file from
Wave-LR."* — on every structural variant (as-is UISP tar, fresh repack, `.cfg`
rename). `ucfg backup restore` over SSH applied the same backup cleanly.

Two traps that cost time here:

1. **The wizard's error names a *model*, not a firmware.** `board.json` said
   `model: Wave-LR` and the hardware genuinely was a Wave-LR. Do not read
   *"Please upload configuration file from Wave-LR"* as a model mismatch, and do not
   repack the tar or flash firmware on the strength of it — go to SSH instead.
2. **Getting to SSH requires the wizard first.** A factory-reset wave has no SSH until it
   takes a DHCP lease, and SSH is off even after the account exists until it's enabled in
   the device UI. Plan for that bootstrap rather than treating SSH as unavailable.

## Waves intermittently refuse rapid repeated SSH logins

A wave will fail consecutive `ssh` invocations with `Permission denied` even with correct
credentials, then accept the next one. This is **not** a wrong-password or expired-lease
problem. Wrap SSH/SCP in a retry loop (3–4 attempts, ~4s apart) instead of re-checking
credentials or re-leasing. Verified on all three radios of the multi-wave job below.

## Verifying a restored wave's backhaul: OSPF `Full` beats ping

A restored wave can show alarming `iwconfig` output that looks like failure:

```
ath0  ESSID:"\x00..."  Access Point: Not-Associated  Link Quality=0/94  Signal level=-93 dBm
```

That is the **5 GHz backup radio** sitting idle. On a Wave-LR the primary link is the
60 GHz radio (`prs0`, carrier=1) — check `carrier` in `/sys/class/net/<iface>/carrier`
rather than reading `iwconfig ath0`. For a backhaul pair, the authoritative check is the
**OSPF adjacency going `Full` on the router** plus the router port at 1Gbps, not ICMP.

**Ping from the correct source address.** A router often cannot ping a radio's management
IP at all (100% loss) yet the radio is perfectly healthy, because the radio's `defaultGateway`
is the far-end router address and replies to *that* source only. Test with an explicit source:
`/ping <radio_ip> count=10 src-address=<radio_defaultGateway>`. Some radios still show
elevated loss from the router's own vantage while showing 0% from an external host — trust
the external measurement plus OSPF `Full`.

## Validated live — three Waves restored in one job

Router `India` (RB5009UG+S+, RouterOS 7.16.1). A power event dropped ether5/6/8
together; three radios factory-reset, the fourth (`india2mode`) came back with config intact
— i.e. one event hit all four, and 1 of 4 kept its config.

| Radio | Port | Subnet used | Original IP | SSID | apMode |
|-------|------|-------------|--------------|------|--------|
| India 2 Lima ST | ether6 | `192.168.1.0/24` | 10.0.60.35/29 | `indiap2p` | false |
| India to Juliet (AP) | ether5 | `192.168.2.0/24` | 10.0.60.26/29 | `india2juliet` | true |
| India 2 Kilo City AP | ether8 | `192.168.<N>.0/24` (N=3) | 10.0.60.10/29 | `kilocityp2p2` | true |

Notes worth carrying forward:
- All three factory radios report `host-name="Wave Long-Range"` in DHCP, so **hostname does
  not identify them** — match on the `mac-address` of the lease to the port's MAC.
- Backup hostname may differ from the ticket/site name (Juliet's was "India to Juliet"),
  so verify identity by MAC, not by name.
- Wizard password complexity rejects the usual wave-restore default (needs ≥12 chars
  and an uppercase letter). The temporary bootstrap account is irrelevant anyway — the
  restored config's `users[]` replaces it. Post-restore SSH used the same pool password.
- `carrierd` (carrier detect) is enabled on the ST with `upDelay=600`; on a Wave-LR it only
  trips once the *wireless* link is up, so a healthy link should not trigger it.

## Wave factory-mode behavior (critical gotchas)

- Factory-reset wave pings at `192.168.1.20` but has **no SSH** — you cannot log in
  until it has grabbed a DHCP lease. Prefer the wizard **Upload Configuration**
  restore above (web UI only — no SSH needed).
- The lease must come from a DHCP server on the router/interface facing the device.
  Standard setup (see SKILL.md Step 3e):
  ```
  /ip pool add name=waveDHCP ranges=192.168.1.100
  /ip dhcp-server add address-pool=waveDHCP interface=<iface> lease-time=5m name=waveDHCP
  /ip dhcp-server network add address=192.168.1.0/24 gateway=192.168.1.1
  ```
  The single-IP pool at `192.168.1.100` is the house standard.
- Lease can take minutes. Poll `/ip dhcp-server lease print` up to ~5 minutes.
- **No lease after ~5 min:** the device is either stuck in **TFTP recovery mode**
  (known bug — will never lease) or needs a **power cycle**. Ask the tower host to
  reboot the radio, then re-poll. Do not leave the DHCP server running indefinitely.

## Upload-path rejections: firmware mismatch + foreign tar format (validated live, `Alpha 2 Upsilon AP` Wave-LR)

When the wizard **and** the Settings → Upload Configuration path both reject a valid UISP backup ("Configuration upload failed" / "Incompatible backup configuration file"), two causes were proven live and fixed remotely:

1. **Firmware mismatch.** A factory-reset wave often boots the factory image (e.g. 4.1.0) while the backup was taken on newer firmware (4.1.1). The upload silently stages but every restore attempt fails until the firmware matches. Fix: the wave's web UI has internet once a DHCP lease + srcnat masquerade exist — download the exact version from `https://fw-update.ubnt.com/api/firmware?filter=eq~~platform~~wave&filter=eq~~product~~GMC&filter=eq~~channel~~release` (use the `_links.data.href`, verify md5), upload via the **Upload Firmware** file input, then click **Reboot Device** and confirm **Upgrade** in the modal (the app detects the staged firmware and offers it there — the staged image does NOT auto-flash). Verify version on Settings after reboot.
2. **Foreign tar backup rejected even on matching firmware** (UISP tar.gz of `config.json`+`board.json` → still "Configuration upload failed"; SSH stayed closed through all of this — web is the only path). Fix: download the device's **own** backup (Settings → Back Up Configuration → `.cfg`, a tar.gz with `config.json` + `board.json`), deep-merge the UISP backup's values into that device-generated `config.json` (scalars/arrays overridden by backup values, device's structural keys retained), and repack reusing the **device's own tar member metadata** (uid/gid/uname/mtime/mode from its tar headers). Upload → **Apply** → device reboots onto its restored static IP in ~30–60s.

Round-trip isolation trick: uploading the device's own just-downloaded `.cfg` back must succeed (Discard/Apply/Test modal). If that succeeds but a repack fails, the problem is your tar metadata or JSON structure, not the flow.

Note: completing the wizard's account-creation step is harmless — the merged config's `users[]` (backup password hash) replaces it on Apply.

## Wave config fields that matter for verification

| Key | Meaning |
|-----|---------|
| `network.interfaces.data.ipv4.cidr` | Original management IP/prefix (e.g. `10.0.41.3/29`) |
| `network.interfaces.data.ipv4.defaultGateway` | Original gateway (lowest usable IP of the `/29`) |
| `system.hostname` | Device hostname (e.g. `quebec2gammaST`) |
| `wireless.ssid` | Backhaul SSID (e.g. `gamma2quebec`) |
| `wireless.apMode` | `true` = AP side, `false` = ST side |
| `services.sshServer.enabled` | Should be `true` post-restore |
