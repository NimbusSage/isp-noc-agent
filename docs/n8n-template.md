# N8N Ticket Workflow Template

This is a scrubbed, importable version of the N8N workflow that feeds tickets
into the webhook server (`POST /webhook/ticket`). The live deployment used
Sonar's webhook → N8N → agent-server chain; yours needs the same three pieces,
whatever your ticketing source is.

## What the workflow does

1. **Webhook** receives the ticketing system's event payload (Sonar posts
   `{ event, body: { current, original } }`).
2. **Filter open tickets** — skips events where `body.current.status == CLOSED`.
3. **Route by group** — a Switch maps your NOC / Sys Admin / other group IDs to
   a `groupName` value used in chat alerts. Replace the hardcoded IDs with yours.
4. **Split by event type** — `ticket.created` sets `ticketCreated=true`,
   `ticket.updated` sets it false.
5. **Dedupe transfers** — updated tickets are only forwarded when the group
   actually changed (`body.original.ticket_group_id != body.current.ticket_group_id`).
6. **Call the agent server** — `POST /webhook/ticket` with Basic auth
   (`TICKET_WEBHOOK_AUTH_TOKEN`), body per the server contract (see
   `docs/webhook-api.md`). Timeout 5s, `onError: continueErrorOutput`.
7. **Fallback chat alert** — if the agent POST errors out, still post a notice
   to your Google Chat space so a new ticket is never silently dropped.

## Import the template

1. In n8n: **Workflows → Import from File**, select `docs/n8n/workflow-template.json`.
2. In the credential dialog, create an HTTP Basic Auth credential
   (`OCAGENT:<TICKET_WEBHOOK_AUTH_TOKEN>` from the agent server's `.env`).
3. Replace every `<PLACEHOLDER>` in the workflow (search the JSON after import):
   - `<NOC_GROUP_ID>` / `<SYSADMIN_GROUP_ID>` — your ticketing group IDs.
     These must match the group IDs the agent expects (config.js `NOC_GROUP_ID`
     and the Sys Admin group that gets passthrough handling).
   - `<AGENT_SERVER_URL>` — host running `server/index.js`, port
     `WEBHOOK_PORT` (default 65333), e.g. `http://10.0.0.5:65333/webhook/ticket`.
   - `<YOUR_CHAT_WEBHOOK_URL>` — Google Chat space incoming webhook.
   - `<YOUR_TICKETING_APP_URL>` — base URL of your ticketing app, used only
     for the "open ticket" link in chat alerts.
4. Copy n8n's **Production webhook URL** into your ticketing system's
   webhook configuration (in Sonar: **Settings → Integrations → Webhooks**,
   events `ticket.created` + `ticket.updated`, pointing the "data" field at the
   full ticket payload).

## Scrubbed workflow JSON

Import target: `docs/n8n/workflow-template.json` (checked in alongside this
doc). Node names are neutral ("Agent Server Call", etc.); the flow is
identical to the deployment this repo was built against.

## Step-by-step build (by hand)

If you'd rather click it together in the n8n UI than import JSON:

1. **Webhook** node → method `POST`, path `sonartickets` (any unique path).
2. **IF** node: `{{ $json.body.current.status }} notEquals "CLOSED"`.
3. **Switch** node (`ticket group`): rules comparing
   `{{ $json.body.current.ticket_group_id }}` equals `<NOC_GROUP_ID>` → output
   "NOC"; equals `<SYSADMIN_GROUP_ID>` → output "SysAdmin".
4. **Set** node per branch: assign `groupName` = `NOC` / `Sys Admin`
   (`includeOtherFields: true` so the payload passes through).
5. **Switch** node (`event type`): `{{ $json.body.event }}` equals
   `ticket.created` → output 0; `ticket.updated` → output 1.
6. **Set** node ×2: `ticketCreated` = `true` (from output 0) / `false`
   (from output 1), `includeOtherFields: true`.
7. **IF** node (`would forward?`): from the `ticketCreated=false` branch,
   condition `{{ $json.body.original.ticket_group_id }}` notEquals
   `{{ $json.body.current.ticket_group_id }}` (number comparison). True → join
   the agent call path; false → stop.
8. **HTTP Request** node (`Agent Server Call`): `POST
   <AGENT_SERVER_URL>/webhook/ticket`, Generic Credential Type → HTTP Basic
   Auth, body parameters `ticket_id`, `subject`, `ticket_group_id`,
   `ticket_group_name`, `status`, `priority`, `description`, `ticket_created`
   (all from `{{ $json.body.current.* }}`, group name from `{{ $json.groupName }}`).
   Timeout 5000 ms, On Error → **Continue (using error output)**.
9. **IF** node (`notify chat on agent failure`): from the error output,
   condition `{{ $json.ticketCreated }}` is true.
10. **HTTP Request** nodes (`create alert` / `transfer alert`): POST to
    `<YOUR_CHAT_WEBHOOK_URL>`, body `text` =
    `An open ticket has been created in the {{ $json.groupName }} group.\n\nSUBJECT:
    {{ $json.body.current.subject }}\n\nURL: <YOUR_TICKETING_APP_URL>` (or the
    "has been transfered to" variant from the transfer branch).
11. Activate the workflow and point your ticketing system at the webhook URL.

The server listens on `127.0.0.1` by default, so n8n on a separate host cannot reach it. To run n8n on a different host: set `WEBHOOK_BIND` on the server host to that host's private IP address, set `WEBHOOK_TLS_PROXY=1`, and restrict port 65333 to the n8n host only in the firewall.

## Verifying end-to-end

```bash
# 1. Agent server up?
curl -s http://<AGENT_SERVER_URL>:65333/health

# 2. Direct POST bypassing n8n (proves the server + auth half)
printf 'header = "Authorization: Bearer %s"\n' "$TICKET_WEBHOOK_AUTH_TOKEN" | curl -s -K /dev/stdin -X POST http://<AGENT_SERVER_URL>:65333/webhook/ticket \
  -H 'Content-Type: application/json' \
  -d '{"ticket_id":"999999","subject":"[Alpha] bravo pppoe - slow tower",
       "ticket_group_id":<NOC_GROUP_ID>,"ticket_group_name":"NOC","status":"OPEN",
       "priority":"Medium","description":"test","ticket_created":false}'
# expect: {"status":"accepted"} within ~1s; processing happens async

# 3. n8n path: create a real test ticket and follow it in the n8n Executions view
```

The server's batching window (60s) then serializes everything through the
FIFO queue — see `docs/webhook-api.md` for the batching semantics and
`server/logs/webhook.log` / journalctl for what the agent actually did.
