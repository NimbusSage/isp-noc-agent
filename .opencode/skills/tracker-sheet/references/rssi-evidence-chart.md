# Tracker Sheet — RSSI evidence chart (reference)

Moved out of `SKILL.md` §13. Post-filing evidence for sharp-signal link tickets. Section numbers elsewhere in the skill still refer to the numbering in `SKILL.md`.

## 13. RSSI evidence chart for misaligned / chain-imbalance / turned link tickets

For every filed row whose Issue is one of the sharp-signal link families — **Link Misaligned
(turned), Chain Imbalance, Faulty Dish/Radio, Radio Separation, Link Downgrade** — post a
follow-up comment on the created Sonar ticket with numeric signal evidence, after
`wait-for-ticket` returns the `Ticket #` (same post-file phase as §12). Purpose: give
dispatch the *shape* of the event (persistent sharp steps = turned/moved; slow drift with
seasonal recovery = foliage) without the operator hand-pulling screenshots, while keeping
the LibreNMS live graph one click away.

**Data source (verified on this install):**

- **Primary numeric series: UISP device statistics** — `GET /nms/api/v2.1/devices/{id}/statistics?interval=<hour|fourhours|day|week|month|quarter|year>`
  (token from the MCP encrypted store, `ubnt-uisp.apiToken`; keys `signal60g` / `remoteSignal60g` for Wave 60G pairs).
  **Retention is LONG, not 30d** — `interval=month` only aggregates a rolling month window, but `interval=year` returned
  Jan→today for this AP (verified; operator has pulled 1-year charts from UISP before). Use `month` for
  sharp recent drops, `year` to check claims about older months. Note: longer windows are coarser buckets (~2-day
  points on `year`), so a persistent band-wide dip whose step is within one bucket may look flat there — cross-check
  `month` for recent events.
- **Device pick: the AP side, unless literally only the ST exists/has good data** (operator rule).
  Resolve the UISP device ids from the NMS device list by mgmt IP; the MyMate `links` cache and
  the device record give both endpoints (`lima2mike` AP 10.0.81.58 ↔ `Lima2Mike ST` 10.0.81.59).
- **LibreNMS CANNOT serve graph images via the API token on this install:**
  `<LIBRENMS_URL>/graph?type=device_wireless_rssi&device=<device_id>&from=<epoch>&height=598&width=2022` and the
  clean `/graphs/type=…` routes 500 without an interactive web session (verified; `/api/v0` has no graphs route).
  The staff/manual reference (requires a logged-in browser) is the operator's usual chart:
  `<LIBRENMS_URL>/graph?type=device_wireless_rssi&device=<device_id>&from=<epoch>&height=598&width=2022`.
  **Authentic LibreNMS render — WORKS (server + client both fixed; verified end-to-end):**
  `--librenms-render` fetches the real LibreNMS-rendered graph. Worked example: a ticket comment with
  two files (LibreNMS native, 1771×754, and the self-render) — DECIDED (operator, after the
  side-by-side): the NocAgent self-render IS the deliverable; the LibreNMS native render is used ONLY as
  a data fallback (see below).
  Client flow (in the script, no manual steps): every request sends the `X-Forwarded-Email: $LIBRENMS_WEB_USER`
  header (name configurable via `CHART_EMAIL_HEADER`; SSO header mode — identity comes from the header; NO password check happens from a trusted source,
  which is why LIBRENMS_WEB_USER must stay a dedicated non-admin account) →
  `GET /graph.php?type=device_wireless_rssi&device=<id>&from=<epoch>` answers `image/svg+xml` on modern
  modules (PNG on legacy types) → ImageMagick `convert` + magic-byte validation, fail closed on anything else.
  `--width/--height` are honored (1100×420 → 1771×754 at density 144).
  **Server-side prerequisite (recorded):** the native render only works when the LibreNMS install runs
  `auth_mechanism=sso` in header mode and the agent host's egress IP is in `sso.trusted_proxies`. That value
  can be SHADOWED: a legacy `$config["sso"]["trusted_proxies"]` line in LibreNMS's `config.php` beats the DB row,
  and a stale Redis/config cache keeps serving the old merged value (every anonymous route then 500s). Fix: comment out
  the legacy line, keep the DB row `sso.trusted_proxies` listing the proxy and the agent's NAT egress IP, then
  `php artisan cache:clear && php artisan config:clear` as the LibreNMS user. KEEP THE TRUSTED LIST TIGHT: anyone
  at a trusted IP can auto-auth as ANY user via the header. If the server breaks differently, reproduce the
  diagnosis from the LibreNMS log (`AuthenticationException @ SSOAuthorizer.php`) and check BOTH shadow layers
  (`config.php` + Redis) before concluding anything.
  **Renderer rule (operator standard):** ALWAYS the NocAgent self-render — UISP first for
  Ubiquiti radios; fall back to the LibreNMS native render ONLY when UISP has no usable data or the
  device isn't in UISP (never render LibreNMS first, never post LibreNMS when UISP data exists).
  The script enforces this: `--librenms-fallback` triggers the native render automatically only when the
  UISP fetch fails/returns no series (also covers device-not-in-UISP HTTP errors), needing
  `--librenms-device` or `--ip` (cache) to resolve the LibreNMS id; result JSON then carries
  `source: "librenms-fallback"` + the UISP error, and `source: "uisp"` otherwise. Verified both paths
  live (bogus-UUID 404 → native render produced; normal device → `source: uisp`, marker
  pixel `255,0,0`).

**Script (deterministic, offline-testable):**

```bash
python3 scripts/rssi_drop_graph.py <uisp-device-id> \
  --device-name "<AP name> (Wave-LR 60G)" \
  --out logs/charts/rssi_<link>.png --json-out /tmp/rssi_result.json
python3 scripts/rssi_drop_graph.py 0 --selftest --no-png   # offline suite
```

It fetches the UISP series, detects persistent step-drops, renders a LibreNMS-style RSSI
PNG (headless Chrome), prints JSON (events with `before_dbm/after_dbm/recovered` + a
timestamped `comment_metrics`), and self-renders without network via `--selftest`.

**Render gotcha (bit us):** the HTML wrapper must use an ABSOLUTE `file:/` URI
(`pathlib.Path(...).as_uri()`). A relative path (`file://logs/charts/….html`) is parsed by
Chrome with `logs` as the HOST, producing a PNG that is a screenshot of the Chrome error
page — a "valid" 21 KB PNG that embeds as a broken picture on the ticket. The script now
stamps a 6×6 pure-red marker at the SVG's top-left corner and `validate_png_pixels()`
crops that exact region via ImageMagick `convert` and asserts 255,0,0 — an error-page
screenshot can never pass. If ImageMagick is missing on a host, FAIL CLOSED (do not embed
an unvalidated PNG); report and let the operator eyeball the file first. The old bad file
must be swapped: upload the corrected PNG, attach it (comment `files` input is ADDITIVE —
the previous file stays listed), update the `<img src="files/<old>">` tag to the new id,
then `deleteFile(<old>)` (detach happens automatically; verified).
Drop semantics: step ≥6 dB below the median of the prior ~24h, with a ~48h
lookforward; `recovered:false` = stayed down = turn signature; `recovered:true` = brief dip
(shown as background tick on the chart only).

**Comment mechanics (verified live):**

- Post via `createTicketComment(ticket_id, body)`; the body is a short fact-text (stable
  baseline range, each persistent step with date + before/after dBm, link-loss date) plus the
  staff LibreNMS graph URL.
- **Sonar strips inline `data:` URI `<img>` tags** (verified — never use them). File upload was
  initially believed impossible via API, but it does exist:
  `POST https://your-org.sonar.software/api/files` with the Bearer token and multipart field
  **`files[0]=@chart.png;type=image/png`** (plural, array-bracketed — `files=@…` gives 422
  "must be an array"). Response: `{"files": [{"id": <file_id>, "stored": true, ...}]}`.
- Then attach + embed on the SAME ticket comment: `updateTicketComment(id: <comment_id>,
  input: { files: { file_id: <file_id>, primary_image: true } })`, and append
  `<p><img src="files/<file_id>" alt="..."></p>` to the body (same relative form the UI
  produces). `createTicketComment` also accepts `files: [{ file_id: ... }]` directly.
  Download/verify: comment `files` connection shows `{id, filename, primary_image}`; the
  sanitizer keeps `files/<id>` img srcs (verified kept).
- Cleanup of probe files: `deleteFile(id)` returns SuccessResponse (no `id` field) — verify by
  re-querying `files(search …)` for `total_count: 0` instead.
- **Email-in was operator-tested and FAILED** as an ingest path — do not pursue.
- Never post machine-garbage in the comment on detection failure — fall back to the staff
  graph URL + `0 cx` style facts.

Worked example: AP `lima2mike` series: stable -40..-50 through day 1, persistent step ~-48→-60 on days 1-2,
slide -38→-66 on days 3-4, collapse past -78, link dark on day 6. Map LibreNMS device ids from mgmt IPs
(cache in `logs/cache/librenms_devices.json`).
