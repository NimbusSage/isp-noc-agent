# N8N Workflow Modification Guidelines

## Overview

This document explains how to modify your existing N8N workflows to integrate with the NOC Agent webhook server, with fallback to Google Chat.

## Architecture

```
N8N Workflow
    │
    ├──► NOC Agent Webhook (primary)
    │    POST https://your-server:65333/webhook/ticket
    │    Authorization: Basic base64(OCAGENT:<token>)
    │
    └──► Google Chat Webhook (fallback)
         POST https://chat.googleapis.com/v1/spaces/<YOUR_SPACE>/messages
         (if agent doesn't respond within 5 seconds)
```

The server listens on `127.0.0.1` by default, so n8n on a separate host cannot reach it. To run n8n on a different host: set `WEBHOOK_BIND` on the server host to that host's private IP address, set `WEBHOOK_TLS_PROXY=1`, and restrict port 65333 to the n8n host only in the firewall.

## Step 1: Create HTTP Request Node for Agent

Add an HTTP Request node in your N8N workflow:

### Node Settings

- **Method:** POST
- **URL:** `https://your-server-ip:65333/webhook/ticket`
- **Authentication:** Basic Auth
  - **Username:** `OCAGENT`
  - **Password:** `<TICKET_WEBHOOK_AUTH_TOKEN>` (from `.env`)
- **Headers:**
  - `Content-Type`: `application/json`
- **Body (JSON):**
  ```json
  {
    "ticket_id": "{{ $json.ticket_id }}",
    "event_type": "{{ $json.event_type }}"
  }
  ```

### Timeout Settings

Set the HTTP Request timeout to **5 seconds** (5000ms):
- **Timeout (ms):** 5000
- **Never Error:** true (so workflow continues on timeout)

## Step 2: Create Fallback Logic

Add an IF node after the HTTP Request to check for timeout/error:

### Condition

```javascript
// Check if agent responded successfully
$json.statusCode === 200 || $json.statusCode === 202
```

### True Branch (Agent Responded)
- Log success
- Continue workflow

### False Branch (Timeout/Error)
- Send to Google Chat webhook

## Step 3: Google Chat Fallback Node

Add another HTTP Request node for Google Chat:

### Node Settings

- **Method:** POST
- **URL:** `https://chat.googleapis.com/v1/spaces/<YOUR_SPACE>/messages?key=<YOUR_KEY>&token=<YOUR_TOKEN>` (from `.env` `NOC_TICKETS_GOOGLE_CHAT_WEBHOOK_URL`)
- **Headers:**
  - `Content-Type`: `application/json`
- **Body (JSON):**
  ```json
  {
    "text": "NOC Ticket Alert",
    "cards": [
      {
        "header": {
          "title": "Ticket #{{ $json.ticket_id }}",
          "subtitle": "{{ $json.event_type }}",
          "imageUrl": "https://example.com/icon.png",
          "imageStyle": "AVATAR"
        },
        "sections": [
          {
            "header": "Details",
            "widgets": [
              {
                "keyValue": {
                  "topLabel": "Ticket ID",
                  "content": "{{ $json.ticket_id }}"
                }
              },
              {
                "keyValue": {
                  "topLabel": "Event Type",
                  "content": "{{ $json.event_type }}"
                }
              }
            ]
          }
        ]
      }
    ]
  }
  ```

## Example Workflow JSON

```json
{
  "nodes": [
    {
      "parameters": {
        "httpMethod": "POST",
        "path": "noc-ticket-webhook",
        "responseMode": "responseNode"
      },
      "name": "Webhook Trigger",
      "type": "n8n-nodes-base.webhook",
      "position": [250, 300]
    },
    {
      "parameters": {
        "method": "POST",
        "url": "https://your-server-ip:65333/webhook/ticket",
        "authentication": "basicAuth",
        "basicAuth": "nocAgent",
        "sendBody": true,
        "bodyParameters": {
          "parameters": [
            {
              "name": "ticket_id",
              "value": "={{ $json.ticket_id }}"
            },
            {
              "name": "event_type",
              "value": "={{ $json.event_type }}"
            }
          ]
        },
        "options": {
          "timeout": 5000
        }
      },
      "name": "Call NOC Agent",
      "type": "n8n-nodes-base.httpRequest",
      "position": [450, 300],
      "credentials": {
        "basicAuth": "NOC Agent Auth"
      }
    },
    {
      "parameters": {
        "conditions": {
          "number": [
            {
              "value1": "={{ $json.statusCode }}",
              "operation": "equal",
              "value2": 202
            }
          ]
        }
      },
      "name": "Agent Responded?",
      "type": "n8n-nodes-base.if",
      "position": [650, 300]
    },
    {
      "parameters": {
        "method": "POST",
        "url": "https://chat.googleapis.com/v1/spaces/<YOUR_SPACE>/messages?key=<YOUR_KEY>&token=<YOUR_TOKEN>",
        "sendBody": true,
        "bodyParameters": {
          "parameters": [
            {
              "name": "text",
              "value": "NOC Ticket #{{ $json.ticket_id }} - Agent did not respond"
            }
          ]
        }
      },
      "name": "Google Chat Fallback",
      "type": "n8n-nodes-base.httpRequest",
      "position": [850, 400]
    }
  ]
}
```

## Testing the Integration

### Test Agent Directly

```bash
curl -X POST https://your-server-ip:65333/webhook/ticket \
  -H "Authorization: Basic $(echo -n 'OCAGENT:<TICKET_WEBHOOK_AUTH_TOKEN>' | base64)" \
  -H "Content-Type: application/json" \
  -d '{"ticket_id":"<ticket-id>"}'
```

Expected Response:
```json
{
  "status": "accepted",
  "message": "Job <uuid> queued for processing",
  "job_id": "<uuid>",
  "source": "ticket"
}
```

### Test Health Check

```bash
curl https://your-server-ip:65333/health
```

Expected Response:
```json
{
  "status": "ok",
  "timestamp": "T01:50:00.000Z",
  "service": "noc-agent-webhook",
  "version": "1.0.0"
}
```

## Monitoring

### View Agent Logs

```bash
# View webhook logs
tail -f logs/webhook.log

# View systemd service logs
journalctl -u noc-agent -f
```

### Check Service Status

```bash
sudo systemctl status noc-agent
```

## Troubleshooting

### Agent Not Responding

1. Check service status: `sudo systemctl status noc-agent`
2. Check logs: `journalctl -u noc-agent -n 50`
3. Test health endpoint: `curl https://your-server-ip:65333/health`
4. Verify .env variables are set correctly

### Authentication Errors

1. Verify token in .env matches N8N credential
2. Check Base64 encoding: `echo -n 'OCAGENT:<token>' | base64`
3. Test with curl directly

### Timeout Issues

1. Increase HTTP Request timeout in N8N (default: 5000ms)
2. Check agent processing time in logs
3. Verify OpenCode CLI is working: `opencode run "test"`

## Security Notes

- Keep the auth token secure
- Use HTTPS in production (with reverse proxy like nginx)
- Monitor logs for unauthorized access attempts
- Rotate tokens periodically
