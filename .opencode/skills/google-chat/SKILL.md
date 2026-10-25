---
name: google-chat
description: Use when sending notifications to Google Chat about NOC tickets that need human attention. Beautiful, uniformly formatted cardsV2 alerts, sent via scripts/chat_alert.py.
---

# Google Chat Notification Skill

## Overview

Send notifications to Google Chat when a NOC ticket needs human attention. All notifications are **cardsV2** — the format Google Chat spaces actually render (cards v1 is deprecated and renders as plain text on many spaces, which is exactly the "ugly alerts" regression).

**Always send through `scripts/chat_alert.py`** — the canonical sender. It builds a consistent, branded card (header icon per severity, bold labeled rows, evidence list, VIEW TICKET button) and never depends on the model hand-rolling JSON. Hand-rolled payloads are why alerts drifted ugly in the first place.

Two format hard rules the helper already follows:
- **No tiny inline icons.** `decoratedText` + `knownIcon` renders as blank squares on many spaces — rows are plain `textParagraph` with `<b>` labels instead.
- **No unverified image URLs.** Most of the old `www.gstatic.com .../system/2x/*.png` icons are 404 now. The helper uses only `fonts.gstatic.com` material-icon SVGs and HEAD-verifies each one at send time (dropping any that stopped serving), so cards never ship a blank header image. Status is always visible via emoji in the title as a fallback.

## When NOT to Send Notifications

**RULE 1: Successfully handled duplicates = NO notification**
- If status is `handled_duplicate` → Do NOT send notification
- These tickets are already resolved (comment posted, reassigned to CS Level 2)
- Notification would be noise

**RULE 1b: IHN / Correlated auto-bounced = NO notification (silent bounce)**
- If status is `auto_bounced_ihn` or `auto_bounced_ihn_peak` or `correlated_bounced` → Do NOT send notification
- Unified/correlated comment posted + moved to CS Level 2; feedback via CS return covers mispredictions — see `ihn-auto-return` / `correlated-auto-return`

**RULE 1c: Queued for peak = NO immediate notification (silent queue)**
- If status is `queued_for_peak` → Do NOT send Google Chat immediately; scheduler at 20:05 CT will retest and then either bounce silently or notify
- See `peak-scheduler`

**RULE 2: Auto-closed tickets = NO notification**
- If status is `auto_closed` (PBS success) or `auto_closed_ooo` (out-of-office reply) or `auto_closed_maintenance` → Do NOT send notification
- These are already resolved and are noise to forward

**RULE 3: Sys Admin passthrough = SEND notification** (forward details)

**RULE 4: Everything else = SEND notification**
- `needs_human`, `categorized` with warnings, API errors, unknown tower site, medium-confidence duplicate (50-84%), healthy-but-low-tower-confidence, conflicting categories, any uncertainty

**Rule summary:**
- Silent: `handled_duplicate`, `auto_bounced_ihn(_peak)`, `correlated_bounced`, `queued_for_peak`, `auto_closed*`
- Everything else → notify via the helper below

## Canonical Sender: scripts/chat_alert.py

```bash
# Ticket alert (the normal case)
python3 scripts/chat_alert.py ticket \
  --ticket-id "$TICKET_ID" \
  --status needs_human \
  --subject "$SUBJECT" \
  --group "NOC" \
  --priority "$PRIORITY" \
  --source "Transferred to" \
  --issue "Unable to determine tower site from subject" \
  --reason "Subject does not match expected format [Network] Site pppoe - <desc>" \
  --analysis "N/A (tower site unknown)" \
  --evidence "Site-name heuristic failed" \
  --evidence "No Sonar tower: note in ticket comments" \
  --confidence 45

# Generic operational alert (breaker, batch failure, restart drop)
python3 scripts/chat_alert.py alert \
  --title "Sonar API unreachable" \
  --subtitle "Batch agent could not fetch ticket details" \
  --severity error \
  --row "Affected:#12345" \
  --row "Error:GraphQL timeout 30s" \
  --button "Tickets:https://$BASE/app#/tickets/show/12345"

# Inspect without sending
python3 scripts/chat_alert.py ticket --ticket-id 1 --dry-run
python3 scripts/chat_alert.py --selftest
```

What it does for you:
- Reads `NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL` from env, or parses `.env` directly (tolerates CRLF endings and quoting)
- Resolves the Sonar base URL from `site-pack/profile.json` (`org.sonar_base_url`) so the **VIEW TICKET** button is a real link — override with `ORG_SONAR_BASE_URL` if running outside the pack
- Maps status → header icon + title: `needs_human`/warnings → amber warning, bad duplicates → content-copy, errors → red error, resolved → green check
- Carries `--evidence` rows, `--confidence`, and structured rows (`--row Key:value`) into decorated rows
- Always includes a plain `text` fallback body for notification previews and mobile

Optional flags: `--analysis` path-analysis one-liner (e.g. `✓ complete — DEGRADED, bottlenecks: 1`), `--ticket-created true|false` (drives the `Created in` vs `Transferred to` wording), `--button "Label:url"` extras.

## Message Format Reference (cardsV2)

The helper produces this shape (shown for reference / debugging only — do not hand-post):

```json
{
  "cardsV2": [{
    "cardId": "noc-ticket-12345",
    "card": {
      "header": {
        "title": "🚨 Ticket #12345 — Needs Human Review",
        "subtitle": "(town) ap pppoe - slow tower",
        "imageUrl": "https://fonts.gstatic.com/s/i/materialicons/warning/v1/24px.svg",
        "imageAltText": "Needs Attention",
        "imageType": "CIRCLE"
      },
      "sections": [
        { "widgets": [
          { "textParagraph": { "text": "<b>Source:</b> Transferred to NOC group" } },
          { "textParagraph": { "text": "<b>Priority:</b> HIGH" } },
          { "textParagraph": { "text": "<b>Issue:</b> Unable to determine tower site from subject" } },
          { "textParagraph": { "text": "<b>Analysis:</b> N/A (tower site unknown)" } }
        ]},
        { "widgets": [
          { "buttonList": { "buttons": [ { "text": "VIEW TICKET", "onClick": { "openLink": { "url": "https://your-org.sonar.software/app#/tickets/show/12345" } } } ] } }
        ]}
      ]
    }
  }],
  "text": "NOC alert: ticket #12345 — Needs Human Review — (town) ap pppoe - slow tower"
}
```

Rules for any hand-built card (avoid unless the helper truly can't express something):
- **cardsV2 only** — never `cards` (v1); include `cardId` and a plain `text` fallback
- Rows are `textParagraph` with `<b>` labels — never `decoratedText` + `knownIcon` (blank square rendering)
- Header images only from verified-live URLs (`fonts.gstatic.com/s/i/materialicons/<name>/v1/24px.svg`); status emoji in the title so the card reads even with no icon
- Escape `&` `<` `>` in user-derived text (subjects/descriptions) before inserting into HTML-ish fields
- One button minimum: the ticket link

## Webhook URL

`NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL` in the environment or `.env`.

## Testing

```bash
python3 scripts/chat_alert.py ticket --ticket-id 1 --dry-run      # inspect payload
python3 scripts/chat_alert.py --selftest                          # offline build test
printf 'url = "%s"\n' "$NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL" | curl -sS -K - -X POST \
  -H 'Content-Type: application/json' \
  -d '{"text":"NocAgent connectivity test"}'                      # raw connectivity only
```
