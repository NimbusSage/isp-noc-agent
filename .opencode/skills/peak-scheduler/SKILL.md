---
name: peak-scheduler
description: Use when a slow-tower ticket requires a peak-window (8-9 PM Central) retest before bouncing — i.e. it is a night/peak ticket and the current time is outside the window — or when the server's peak timer fires and queued tickets must be re-analyzed during the window. Covers queue file, CT-window math, off-peak enqueue, peak retest → IHN bounce, and verification.
---

# Peak Scheduler — Night/Peak Retest at 8-9 PM Central

Night/peak tickets (`NOC - Night Checks`, `sonar.categories.night_peak`, or subject/comments containing `night check`/`peak hours`) must be **proven healthy during peak congestion** to be bounced. If it's not peak now, enqueue; if it is peak, run the real test now.

## Core Rule (from user)

> Only exclude/bounce-block a night/peak ticket **when outside the peak window**. If we ARE inside 20:00-21:00 CT and the ticket is night/peak, run the analyzer at peak — if still HEALTHY (perfect green, zero warnings) that **satisfies** the night-check requirement, so we MAY bounce it to CS immediately.

This ties the `ihn-auto-return` Gate C to actual peak verification.

## Queue File

- **Path**: `logs/pending_peak_checks.json` (sibling of `logs/pending_tickets.json:36`, lives under `LOG_DIR` env or `<repo>/logs`). Atomic write via `tmp`+`rename` like `pending_tickets.json:65`.
- **Shape**: JSON array of objects:

```json
[
  {
    "ticket_id": "<ticket-id>",
    "tower_site": "tau",
    "analyzer_site": "Tau",
    "search_last": "tau",
    "enqueued_at": "T18:00:00.000Z",
    "reason": "night_peak_off_window",
    "subject": "[Papa] Tau - Slow tower",
    "attempts": 0
  }
]
```

- `tower_site` = parsed site (`ticket-context`), `analyzer_site` = exact name from `GET /api/sites/search?q=<last>` at enqueue time (so scheduler doesn't re-guess). `search_last` kept for re-search if needed.
- Operations: `readPeakQueue`, `writePeakQueue`, `enqueuePeakCheck`, `dequeuePeakCheck`, `clearPeakQueue`. All guarded by file lock via `fs.existsSync` + retry.

## Server Responsibilities (`server/index.js`)

### Env

- `PEAK_WINDOW_TZ` default `America/Chicago`
- `PEAK_WINDOW_START` default `20:00` (24h local)
- `PEAK_WINDOW_END` default `21:00`
- `PEAK_QUEUE_FILE` overrides path if needed (default `LOG_DIR/pending_peak_checks.json`)
- `IHN_TOWER_THRESHOLD` reused for bounce during peak (default 80)
- Optional: `PEAK_SCHEDULER_ENABLED` default `true`, `PEAK_RETEST_SPACING_MS` default `600000` (10 min between sites to avoid analyzer concurrency; spacing handles load-balanced 5-9 min runs without thrashing)

### CT window math

Never use UTC. Compute in `America/Chicago` via `Intl.DateTimeFormat` or `TZ` env:

```js
function isPeakWindow(now = new Date()) {
  const tz = process.env.PEAK_WINDOW_TZ || 'America/Chicago';
  const start = process.env.PEAK_WINDOW_START || '20:00';
  const end = process.env.PEAK_WINDOW_END || '21:00';
  const fmt = new Intl.DateTimeFormat('en-US', { timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false });
  const hm = fmt.format(now); // "20:05"
  return hm >= start && hm < end;
}
```

`server` should also expose `minutesUntilPeak()` to schedule the next timer via `setTimeout`, not `setInterval` fixed to wall clock, so drift and DST are correct:

```js
function msUntilNextPeak() {
  // compute next 20:00 CT as absolute Date, handle DST
}
```

### Timer

On `app.listen` start:

```js
scheduleNextPeakCheck();
```

Implementation sketch (no new deps):

```js
let peakTimer = null;
let peakRunning = false;

function scheduleNextPeakCheck() {
  if (peakTimer) clearTimeout(peakTimer);
  const ms = msUntilNextPeak();
  log('Peak scheduler: next window in', { ms_until_peak: ms });
  peakTimer = setTimeout(async () => {
    await runPeakQueueDrain();
    // schedule next day's window
    scheduleNextPeakCheck();
  }, ms);
  peakTimer.unref?.();
}

async function runPeakQueueDrain() {
  if (peakRunning) { log('Peak drain already running, skipping'); return; }
  const queue = readPeakQueue();
  if (queue.length === 0) { log('Peak window: queue empty'); return; }
  // Early exit if no longer in window (e.g. DST edge)
  if (!isPeakWindow()) { log('Peak drain fired outside window, rescheduling'); scheduleNextPeakCheck(); return; }
  peakRunning = true;
  log('Peak drain starting', { queued: queue.length });
  // Process sequentially with spacing
  for (let idx = 0; idx < queue.length; idx++) {
    const item = queue[idx];
    // Check weather exclusion hasn't become true since enqueue (fetch fresh comments) — if now weather, dq and notify
    try {
      await processPeakItem(item); // see below
    } catch (e) { log('Peak item failed', { ticket_id: item.ticket_id, error: e.message }); }
    // Dequeue processed item (even on failure — don't retry forever; track attempts)
    const q2 = readPeakQueue();
    writePeakQueue(q2.filter(q => q.ticket_id !== item.ticket_id));
    if (idx < queue.length - 1) {
      if (!isPeakWindow()) { log('Peak window ended mid-drain, pausing til next window'); break; }
      const spacing = parseInt(process.env.PEAK_RETEST_SPACING_MS, 10) || 600000;
      await new Promise(r => setTimeout(r, spacing));
    }
  }
  peakRunning = false;
}
```

On `gracefulShutdown`, clear `peakTimer`.

### Off-peak enqueue (called from agent prompt path)

When agent evaluates Gate C and finds `night_peak && !isPeakWindow`, it should call the **Sonar-facing** path (no server helper needed in non-interactive mode — the agent enqueues by appending to the queue file via a bash JSON edit). But the **server** should also handle direct agent instructions: the agent's prompt will say:

```
If night/peak and NOT in peak window:
  1. Do NOT attempt IHN bounce now.
  2. Append {ticket_id, tower_site, analyzer_site, ...} to logs/pending_peak_checks.json (create if missing, use jq/python to atomically append)
  3. Add the night/peak category if not present? Keep original categories, just log.
  4. Post a single comment: "Peak-window retest queued — {analyzer_site} will be re-analyzed between 8-9 PM CT. Path not yet tested at peak." + current off-peak path? Or skip analyzer entirely off-peak to save time?
```

**Performance choice**: Two options for off-peak night tickets:

- **Option A (cheaper, recommended for v1)**: **Skip bottleneck analysis entirely** off-peak for night/peak tickets — just enqueue. Don't waste a 2-9 min off-peak run that won't qualify for bounce anyway. Agent will return `queued_for_peak` with `bottleneck_analysis: "deferred_to_peak"`.
- **Option B (informational)**: Run analysis off-peak anyway and post path, then ALSO enqueue for second peak run. More API load but gives NOC data now. Choose A for perf; BM fallback if operator wants off-peak visibility.

Document chosen Option A in skill and AGENTS.

### Peak retest path (`processPeakItem`)

Called inside the window by scheduler (server-spawned OpenCode run, not normal batch). It spawns a **dedicated OpenCode prompt** per item (or one batch with serial analyzer runs). Simpler: scheduler spawns one `opencode run` per ticket sequentially with prompt:

```
Re-test peak ticket #{ticket_id} for {analyzer_site} at peak (window {start}-{end} CT).
Steps:
1. Re-fetch ticket + comments + categories to NOTE weather state (weather category OR rain|storm|weather keywords OR LLM reasoning). LOG weather_excluded=true/false but DO NOT cancel analysis — per the Gate B fix in `ihn-auto-return`, scheduled peak analysis runs regardless of keyword exclusions (rain-fade tuning comment must not cancel peak run).
2. Re-resolve analyzer_site via GET /api/sites/search?q=<last> (verify peak scheduler pick still correct; confidence re-scored)
3. Run bottleneck analyzer (same search→analyze→poll single-loop 36x15s) at peak. ALWAYS run — do not skip due to weather/keywords.
4. Post SINGLE unified comment:
   - If perfect HEALTHY (Gate D from ihn-auto-return) and tower_conf >= threshold AND weather_excluded==false → run the bounce gate FIRST via `python3 scripts/sonar_bounce.py bounce <id> --reason IHN --add-categories <cx_th_category_id> --remove-categories <slow_speeds_id>,<night_peak_id> --confidence <tower_conf> --evidence "<csv evidence>"` (dry-run first; gate BEFORE the comment — same ordering lesson as ihn-auto-return), then post to CS comment text with marker "Peak Window Retest (8:xx PM CT) — HEALTHY — auto-bounced as IHN", silent, status auto_bounced_ihn_peak
   - If HEALTHY but weather_excluded==true → do NOT bounce, post HEALTHY path with note "HEALTHY but weather keywords present — not bouncing, peak analysis posted for NOC", status needs_human/categorized, notify
   - If DEGRADED/WARNING or failed → post degraded path + correlation (even if weather present — analysis still posted), keep in NOC, status needs_human/categorized, notify Google Chat with peak result + weather_excluded flag
   - If low tower_conf → do not bounce, post path with low-confidence note, needs_human
5. Return JSON [{"ticket_id": "...", "status": "...", "peak_retest": true, "weather_excluded": true/false, ...}]
```

Spacing 10 min ensures one analyzer session at a time.

### Persistence & Recovery

 - File `pending_peak_checks.json` persists across restarts like `pending_tickets.json:47`. On startup, do NOT auto-drain if outside peak — just leave queue. The next scheduled `peakTimer` will drain.
- On `SIGTERM` with queue non-empty, do NOT drop — just persist (no Google Chat alert needed; queue is expected to wait til next peak). Only alert if queue grows large (e.g. >50 stale) or an item ages >7 days.
- Dequeue after success/failure to avoid infinite loop. Track `attempts` — increment on failure, drop after 3 attempts with `failed_permanent`.
- **Single peak test per ticket**: once a ticket has a `Peak Window Retest` analysis comment posted (verified via `--verify`), it is removed from queue and will not be re-enqueued even if the ticket remains open and receives another webhook — the batch prompt checks `ticket_comments` for the marker before enqueueing and skips with `Peak retest already completed`. This is the anti-loop that stops "testing over and over" after the job is done.

### Interaction with IHN skill

- `ihn-auto-return` Gate C delegates to this skill when off-peak.
- Inside peak, `ihn-auto-return` handles bounce decision directly (the scheduler reuses its Gates A-D + scoring).
- Both share `IHN_TOWER_THRESHOLD`, peak window env, and unified comment template (peak version adds `Peak Window Retest (8:xx PM CT)` header).

## What NOT to do

- Do not run analyzer on night/peak tickets **off-peak** just to bounce — defeats peak semantics (would hide peak congestion).
- Do not re-enqueue a ticket already in the queue (dedupe by ticket_id) **and do not re-enqueue a ticket that already has a `Peak Window Retest` comment** (history dedupe — job already done). Check `ticket_comments` DESC 20 for marker `Peak Window Retest` before enqueue; if found, skip and return `needs_human` instead of `queued_for_peak`. This prevents testing over and over every day after the scheduled peak job completed (covers all future tickets).
- Do not spawn parallel analyzer sessions — process queue strictly serially with spacing (the analyzer host is single-threaded enough).
- Do not use `setInterval(24h)` without CT-aware `msUntilNextPeak` — DST will drift.

## Verification

- `node --check` passes, file exists and is `[]` after dequeue.
- `TZ=America/Chicago date` inside window → `isPeakWindow()` true.
- Off-peak: post ticket with `[NIGHT CHECK]` → webhook.log shows `Enqueued for peak` and queue length 1.
- At 20:05 CT (or force by setting `PEAK_WINDOW_START=00:00` in test): queue drains, analyzer session fetched, comment posted with peak header, ticket moved to the CS Level 2 group when HEALTHY.
- No phantom timer after SIGTERM: timer cleared and not holding event loop (`unref`).

