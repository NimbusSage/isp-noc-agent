# AP + CPE Migration (rename-site)

CPE → AP links are cut-and-dry compared to backhauls (one AP, N stations, no gateway move), but volume is higher and some CPEs never reconnect until rebooted. Order + credential hygiene carry the safety.

## 1. CPE discovery (read-only)

For each AP to be renamed:

1. Read the AP registration/association table: MT `/interface wireless registration-table print detail`, airOS `stations` (`sta.cgi`), Wave client list. Record hostname/MAC/signal per client — identities, not just counts.
2. Resolve each client's management IP on the site's PPPoE router:
   - `/ppp active print` (PPPoE sessions: user/MAC/address),
   - `/ip arp print` (MAC → IP),
   - `/ip dhcp-server lease print` (DHCP clients; the rare DHCP CPE),
   - match on MAC. PPPoE covers nearly all; DHCP a small minority.
3. Clients with no IP match are `unresolved` — listed, skipped, never guessed.

## 2. Order: CPEs first, AP last

1. Stage NEW SSID on **every reachable CPE** (supplicant profile SSID; security/PSK untouched — same key, new SSID). CPEs drop as staged — expected.
2. Read back each CPE's staged SSID.
3. Update the **AP SSID identically**. CPEs reassociate on their own over minutes.
4. Verify per CPE: reassociated (registration table), signal near baseline, management pingable, customer path to internet.
5. Non-reconnects: allow a bounded observation window, then one remote reboot if authorized; else report `needs customer reboot` with per-CPE evidence. Safe-mode revert on the AP is immediate if the failure is systemic rather than per-CPE.

## 3. Mixed plant

- MikroTik APs: wireless `ssid` + identical security profile (open-auth PPPoE sectors: no key handling; WPA sectors: key never pasted — file-to-file only per established transport practice).
- UBNT airOS APs: `wireless.1.ssid`; CPE supplicant `wpasupplicant.profile.1.network.1.ssid` (+ `key_mgmt`/`psk` only if the AP is secured AND the key path requires it — normally untouched on pure rename).
- Wave APs: `wireless.ssid` via `ucfg`; CPE stations follow their own supplicant path.
- One AP domain at a time, serially. Never rename two APs sharing a CPE pool concurrently.

## 4. Credentials

CPE logins use the separate customer pool only — see `site-pack/rename-site/cpe-credentials.md` (example: `site-pack/example/cpe-credentials.example.md`; if absent, ask the operator which customer pool to use). Never the infra pool, never chat/logs, never type-wide fallback for writes.
