# Webhook API & N8N Integration

> Operator manual index lives in the repo root [README](../README.md).

## 8. Webhook API Reference

### `POST /webhook/ticket`

Receives a ticket for processing. Returns 202 immediately; processing is async.

**Authentication:** Basic auth — `Authorization: Basic base64(OCAGENT:<TICKET_WEBHOOK_AUTH_TOKEN>)`

**Request body:**

| Field | Required | Description |
|-------|----------|-------------|
| `ticket_id` | Yes | The Sonar ticket ID to process |
| `subject` | No | Ticket subject line (avoids an API call if provided) |
| `ticket_group_id` | No | Sonar group id your org routes NOC tickets to |
| `ticket_group_name` | No | Group name |
| `status` | No | Ticket status |
| `priority` | No | Ticket priority |
| `description` | No | Ticket description |
| `ticket_created` | No | `true` = created directly in NOC group; `false` = transferred |

**Example:**
```json
{
  "ticket_id": "<ticket-id>",
  "subject": "[Bravo] golf pppoe - slow tower",
  "ticket_group_id": <noc_group_id>,
  "ticket_group_name": "NOC",
  "status": "OPEN",
  "priority": "Medium",
  "description": "Customer reporting slow speeds...",
  "ticket_created": false
}
```

**Response:** `202 Accepted`
```json
{"status":"accepted","message":"Job <uuid> queued for processing","job_id":"<uuid>","source":"ticket"}
```

### Other webhook sources

| Endpoint | Handler |
|----------|---------|
| `POST /webhook/network-event` | Network event analysis |
| `POST /webhook/alert` | NOC alert processing |
| `POST /webhook/<anything-else>` | Generic passthrough to agent |

### `GET /health`

Returns service status JSON. No auth required.

### Response codes

| Code | Meaning |
|------|---------|
| 202 | Ticket received, processing started |
| 400 | Missing `ticket_id` |
| 401 | Missing authorization header |
| 401 | Invalid credentials |
| 500 | Server error (check logs) |

### Server address, authentication, and limits

The server listens on `127.0.0.1` by default. Set `WEBHOOK_BIND` to bind to another address. If that address is not loopback and `WEBHOOK_TLS_PROXY=1` is not set, the server logs a warning that the token travels in cleartext; set `WEBHOOK_TLS_PROXY=1` when a TLS proxy or VPN sits in front of the port.

The server accepts `Authorization: Basic base64(OCAGENT:<token>)` (the username is ignored) or `Authorization: Bearer <token>`. Every authentication failure returns 401 with a generic body. `TICKET_WEBHOOK_AUTH_TOKEN` must be at least 24 characters and must not look like a placeholder, or the server exits at startup. Run `node server/index.js --check-config` to validate settings without starting the server; it prints no values.

`/health` and `/healthz` require no token and return no sensitive data. `/governance` and `POST /governance/breaker` require the same token as the webhook.

A ticket id that is already queued or being processed returns 202 with status `duplicate_ignored`; a re-sent webhook with that id does nothing. `ticket_id` accepts 1 to 64 characters from `A-Za-z0-9._:-`.

Limits:

- Body over 256 kb: 413 (configure with `WEBHOOK_MAX_BODY`)
- More than 120 requests per minute from one address: 429 (configure with `WEBHOOK_RATE_LIMIT`)
- Queue at 500 tickets: 503 (configure with `WEBHOOK_QUEUE_MAX`)

### Batching

Tickets received within a **60-second collection window** are batched and processed in a single OpenCode run. This includes all ticket types (NOC, Sys Admin, PBS). Benefits: fewer agent spawns, better duplicate detection across related tickets, lower system load.

**Serial processing:** Batches run **one at a time** through a FIFO queue. A new batch starts only when the previous one settles (success, failure, or timeout). Tickets that arrive while the queue is busy still get `202 Accepted` immediately and wait their turn. Only an OpenCode failure stops the pipeline — genuine failures send a fallback notification and the queue advances to the next batch.

**Restart safety:** Every ticket ID that is queued or in-flight is mirrored in `logs/pending_tickets.json`. On graceful shutdown with tickets still pending, a Google Chat alert lists them and the file is cleared. On startup, any leftover IDs from an unclean shutdown trigger the same alert, so a crash never silently drops a ticket. Alerts are best-effort; there is no automatic queue replay.

**Peak queue:** Night/peak tickets received outside 8-9 PM CT are deferred to `logs/pending_peak_checks.json`. The server fires `runPeakQueueDrain()` at the next 20:05 CT (CT-aware timer via `msUntilNextPeak()`), then drains the queue serially with 10 min spacing, spawning one peak-retest OpenCode run per ticket. Weather is re-checked at peak; HEALTHY at peak → silent IHN bounce (`auto_bounced_ihn_peak`), DEGRADED → correlation + human alert. Queue persists across restarts; outside window it just waits.

---


---

## 10. N8N Integration

The N8N workflow sends tickets to the webhook server with a 5-second timeout. If the agent doesn't respond, N8N falls back to a direct Google Chat notification.

Full setup instructions (HTTP Request node config, fallback logic, example workflow JSON) are in [docs/n8n-integration.md](../docs/n8n-integration.md).

**Quick test:**
```bash
source .env
curl -X POST http://localhost:65333/webhook/ticket \
  -H "Authorization: Basic $(echo -n "OCAGENT:$TICKET_WEBHOOK_AUTH_TOKEN" | base64)" \
  -H "Content-Type: application/json" \
  -d '{"ticket_id":"<ticket-id>"}'
```

---



---

## Send a test ticket (Mode B end-to-end check)

(From the original README quickstart; run on the box where `noc-agent.service` runs.)

```bash
source .env
curl -X POST http://localhost:65333/webhook/ticket \
  -H "Authorization: Basic $(echo -n "OCAGENT:$TICKET_WEBHOOK_AUTH_TOKEN" | base64)" \
  -H "Content-Type: application/json" \
  -d '{"ticket_id":"000000","subject":"[TestSite] test - slow tower","ticket_group_id":<your_noc_group_id>,"ticket_group_name":"NOC"}'
```

Expect `202 Accepted`; watch the batch in `journalctl -u noc-agent -f` and the webhook log.
