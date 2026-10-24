---
name: wave-gui
description: Use when any workflow needs live data from an Ubiquiti Wave (WaveOS gen3) radio's web UI — link state, 60 GHz signal dBm, MCS/modulation, capacity scores, carrier-drop state, or the radio's built-in speed test — via `scripts/wave_gui.mjs` (`read` / `speedtest` / browser `probe`) with the credstore `ubnt-wave` pool. Reused by `path-analyzer` rain-fade-readiness and any other skill that must read a Wave link.
---

# Wave GUI — WaveOS gen3 Web UI/API Driver

Helper skill: returns data, never posts to Sonar, never mutates radio config. The only
mutation this skill ever performs is `enable-ssh` through `wave_gui.mjs` **with its own
`--execute` flag and an explicit operator request**. Everything else is read-only.

## Connection rules (no new methods)

- Radio **config truth** comes through the NOC MCP server (`mcp-server/dist/tool-cli.js run-diagnostics '{"deviceType":"ubnt-wave","host":"<mgmt_ip>","command":"ucfg-show"}'`), which reads `mcp-server/config/credentials.enc` itself — not from this skill.
- Radio **live dashboard state** comes through `scripts/wave_gui.mjs` (see below), which reuses the same encrypted credstore pool (`ubnt-wave`) — never a new credential system.
- Wave radios are ubnt hardware with LibreNMS `os=airos-af60` (`location` free-form). Discovery of *which* IP is Wave-family uses that LibreNMS OS plus MyMate device rows; the site→radio mapping stays org data (`site-pack/devices.md`).

## Credentials

`wave_gui.mjs read|speedtest` resolves credentials in this order:

1. Explicit env override: `WAVE_GUI_USER` / `WAVE_GUI_PASS` (operator-supplied per run).
2. The encrypted MCP credstore `ubnt-wave` pool (priority order) via `scripts/cred_store.mjs`; passphrase is `NOC_MCP_PASSPHRASE` from the environment or repo `.env` — never echoed, never on a command line.

The browser modes `probe` / `enable-ssh` require the env pair. The gen3 web UI password
may differ from an SSH password on the same radio — the credstore `ubnt-wave` pool holds
the credentials that actually authenticate `POST /api/v1.0/user/login`; verify by running
`read` once and checking `loginCredName`.

## Term: "the GUI"

The gen3 app is a Vue SPA served over HTTPS (self-signed cert). Its dashboard data is the
JSON API at `/api/v1.0/...`, authenticated by an `x-auth-token` response header from
`POST /api/v1.0/user/login {username,password}`. `read` and `speedtest` speak that API
directly (no browser); `probe` and `enable-ssh` drive a real browser (puppeteer + system
Chrome, screenshots under `logs/gui/<session>/`).

## `read` — one-shot dashboard truth

```bash
node scripts/wave_gui.mjs read --url https://<mgmt_ip>
```

Returns one JSON object (stdout). Live-verified field map:

| Field | Meaning |
|-------|---------|
| `device.{model,product,firmware,mac,family}` | identity (family=wave) |
| `radios.main.linkState` | `connected` / `active` etc. |
| `radios.main.frequencyCenterMhz` | e.g. `69120` (60 GHz); backup radio frequency is in GHz-class numbers, `0` when disabled |
| `radios.main.channelWidthTxMhz` | TX channel width (`2160` on Wave Pro main) |
| `radios.main.{conductedTxDbm,eirpDbm}`, `antennaGainDbi` | power + antenna (antenna `type built_in`) |
| `radios.main.waveAi` | WaveAI auto-tuning engaged |
| `radios.main.localPeer.signalDbm` | **the composite 60 GHz signal number the dashboard shows** (e.g. −60) |
| `radios.main.localPeer.{signalDayDbm,idealSignalDbm}` | day-averaged signal + ideal (no-fade) signal |
| `radios.main.localPeer.{minSignalDbm,maxSignalDbm}` | histogram bounds (shares) |
| `radios.main.localPeer.mcs` | `rxIdx/txIdx` current, `rxIdxIdeal/txIdxIdeal` ideal, `rxLabel/txLabel` modulation class (e.g. QPSK) |
| `radios.main.localPeer.capacityMbps` | `combined` vs `combinedIdeal` (and `dl/ul` vs `dlIdeal/ulIdeal`) — **Mbps after kbps→Mbps conversion** |
| `radios.main.localPeer.linkScore` | ul/dl/dl24/ul24 scores 0-100 |
| `radios.main.localPeer.gps` | lat/lon/alt of the radio (this end) |
| `radios.backup.*` | 5 GHz backup radio state — `linkState: "disabled"` means the backup is OFF (class-B link, see rain-fade-readiness) |
| `carrierDrop.dropped` | live carrier-drop state (config lives in `ucfg show` via MCP: `network.carrierd.*`) |
| `peerMacs[]` | peer radio MAC(s) minus this device's own MAC (own MAC also at `device.mac`) |

Both link ends should be read independently (the token is per-device; each run logs in
again). The local view's `signalDbm` is this end's own receive metric for the peer.

## `speedtest` — the radio's built-in capacity test

```bash
node scripts/wave_gui.mjs speedtest --url https://<mgmt_ip> [--duration 30] [--direction both|rx|tx] [--mac XX:XX:....]
```

- Drives the same endpoints the gen3 Tools→Speed-Test page uses:
  `POST /api/v1.0/tools/airos/speedtest/start {mac,duration,direction}` → poll
  `GET /api/v1.0/tools/airos/speedtest` → `POST .../stop {mac}`.
- `direction` values `rx`, `tx`, `dx` (duplex) — the endpoint **rejects `all`**; `both` (script-level) runs rx then tx sequentially.
- Duration is seconds per direction (1..600, default 30). Speed numbers are **Mbps**. Status lifecycle: `running` → `completed`.
- Peer MAC auto-derivation when `--mac` omitted: MACs found inside `peers` minus own `device.mac`; ambiguous/none → error telling you to pass `--mac`.
- Non-destructive: traffic only; nothing is persisted to the radio.

Running the test *from the AP* (AP mgmt IP as `--url`) measures the link from the AP.

## Browser modes (retained)

- `probe --url https://H [--click "Text"]… [--session NAME]` — login via one-page
  `page.evaluate` (the Wave UI re-renders the login DOM mid-type, so stale element handles
  break naive automation), screenshots + element maps.
- `enable-ssh --url https://H --execute` — settings page → SSH Server checkbox
  (`#sshServer\.enabled`, CSS-escaped dot), sticky Save Changes; **mutates only with --execute**.

## Errors / traps

- `401` on all pool creds: the gen3 web password differs from the SSH pool; get it from the operator (env pair or store entry). The legacy `/api/auth` route is not the gen3 login and unauthenticated GETs 401 — not evidence of wrong credentials.
- Login HTTP 400: malformed body/framing — verify via `node --check scripts/wave_gui.mjs` and recent edits, not the radio.
- Tokens are per-device and expire; each `read`/`speedtest` run logs itself in.
- Radios respond on their WiFi-management IP (`network.interfaces.data.ipv4.cidr` in `ucfg show`); `https://<ip>` requires trusting self-signed cert (script sets `rejectUnauthorized:false`; browser uses `--ignore-certificate-errors`).
- Re-run politeness: `speedtest` saturates the radio for the requested duration(s); coordinate with operations before running on links carrying live traffic (radio isolation is not guaranteed).

## Env

| Variable | Purpose |
|----------|---------|
| `WAVE_GUI_USER` / `WAVE_GUI_PASS` | explicit credential override (never printed/logged) |
| `NOC_MCP_PASSPHRASE` | credstore passphrase (env or `.env`) |
| `WAVE_DEVICE_URL` | default `--url` |
| `CHROME_PATH`, `CHROME_NO_SANDBOX` | browser mode only |
| `WAVE_TOKEN_OUT` | write captured session token to file (browser mode) |
