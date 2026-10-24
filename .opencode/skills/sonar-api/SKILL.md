---
name: sonar-api
description: Use when making read or write API calls to Sonar — querying customer accounts, radio/inventory records, services/status, tickets/comments, or troubleshooting GraphQL. Covers authenticated schema-verified query patterns, auth, pagination, mutations, and HTML escaping.
---

# Sonar API (Tickets, Accounts, and Inventory)

## Endpoint and Auth

- **Endpoint:** `POST https://your-org.sonar.software/api/graphql`
- **Auth:** Bearer token in `Authorization` header (stored in `SONAR_TOKEN` env var)
- **Interactive explorer:** `https://your-org.sonar.software/graphiql` (UI only, not the API)

### SONAR_TOKEN

Always available in `.env` at the repo root. Source it before API calls:

```bash
export $(grep SONAR_TOKEN .env | xargs)
```

Or in Python:

```python
import subprocess
result = subprocess.run(['bash', '-c', 'source .env && echo $SONAR_TOKEN'], capture_output=True, text=True)
token = result.stdout.strip()
```

## Key IDs (org-specific — resolve from the site pack, never hardcode here)

Sonar group and category IDs are org-specific. Resolve them at runtime from the gitignored local
`site-pack/profile.json` (`sonar.groups`, `sonar.categories`) with env overrides:

| Entity | Resolution | Env override |
|--------|------------|--------------|
| NOC group | `sonar.groups.noc.id` | `NOC_GROUP_ID` |
| Sys Admin group | `sonar.groups.sys_admin.id` | `SYSADMIN_GROUP_ID` |
| CS Level 2 group | `sonar.groups.cs_level_2.id` | `CS_LEVEL2_GROUP_ID` |
| Slow Speeds category | `sonar.categories.slow_speeds.id` | — |
| Night Checks category | `sonar.categories.night_peak.id` | — |
| CX/TH-side Issue category | `sonar.categories.cx_th_side_issue.id` | — |
| Routing Issue category | `sonar.categories.routing.id` | — |
| Web Access Issue category | `sonar.categories.web_access.id` | — |
| Rain Fade category | `sonar.categories.weather.id` | — |

In the examples below, `<noc_group_id>` and `<cs_level_2_id>` are these resolved values.

## NOC Ticket Categories

Category NAMES are org-configured Sonar labels; keep the semantic roles (slow speeds, night/peak,
CX-side, routing, web access, weather) mapped per-install in the site-pack profile.


## Schema Gotchas (differs from Sonar's GraphiQL examples)

- `SearchStringField` uses `match` (Boolean) + `search_value`, NOT `operator`
- `SearchIntegerField` uses `operator` (EQ/GTE/LTE etc.) + `search_value`
- Ticket has `user` (creator), NOT `assignee`
- Ticket has `ticket_categories` (connection), NOT `ticket_category`
- Pagination fields: `page_info { total_count, page, total_pages, records_per_page }` (snake_case)
- Enum values are uppercase: status = `OPEN`/`CLOSED`/etc., priority = `LOW`/`MEDIUM`/`HIGH`/`CRITICAL`

## Customer and CPE / Radio Inventory Lookup (Read-Only, Schema-Verified)

Sonar GraphQL introspection confirmed these relevant types/fields on the current tenant:

- Root `accounts` accepts `general_search`, `search`, and `paginator`.
- `Account` exposes `name`, `account_status { id name activates_account }`, and `account_services`.
- `AccountService` exposes `inventory_items`; its introspected fields include service/package assignment and IDs but no separate per-assignment activation/status field. `Service.enabled` is on the shared catalog definition, not the customer assignment.
- `InventoryItem` exposes `status`, `overall_status`, `inventory_model { name }`, and `inventory_model_field_data`.
- Inventory-model fields include `NAME`, `MAC_ADDRESS`, and `SSID` on the verified LiteAP GPS sample.
- Authenticated query testing confirmed that `accounts(search: [{string_fields: [{attribute: "name", search_value: $term, match: true}]}])` is the proper account-name search shape; `accounts.general_search` is broader and did not find the radio-handle sample. `match: true` remains substring matching, so exact-normalized post-filtering is required.
- Radio-name inventory lookup uses `inventory_model_field_data(search: [{string_fields: [{attribute: "value", ...}]}])`; the queried result connection exposes no reverse `inventory_item` relation in the tenant schema. For linked customer data, traverse the forward path `Account → account_services → inventory_items → inventory_model_field_data`. Search and linkage results remain dependent on correct input identity; do not assume a CPE inventory item is linked to an account.

### Search inventory by radio name

Use `inventory_model_field_data` to match the custom inventory radio-name value and inspect the field's name to ensure the match is a `NAME` value. Search is substring matching, so post-filter the returned `value` in code for the desired exact normalized radio handle. This root connection does not provide an `inventory_item` reverse relation on the live tenant schema; use the forward account → service → inventory query below when establishing customer ownership. Keep `records_per_page` bounded and paginate if `page_info.total_count` exceeds the requested page.

```graphql
query RadioNameInventoryLookup($radioName: String!) {
  inventory_model_field_data(
    search: [{ string_fields: [{ attribute: "value", search_value: $radioName, match: true }] }]
    paginator: { page: 1, records_per_page: 20 }
  ) {
    entities { value inventory_model_field { name } }
    page_info { total_count page records_per_page }
  }
}
```

After fetching, accept a radio-name hit only when `inventory_model_field.name` is the radio field `NAME` and normalized `value` exactly equals the requested CPE handle (case-insensitive; do not strip/merge distinguishing suffixes). If there are zero or multiple exact NAME hits, return unresolved/ambiguous. Do not treat `inventory_item.status` or `overall_status` as customer service status.

### Search customer/account by name and inspect linked services/inventory

Use `accounts.search` with a `string_fields` condition on `name` (not the broad `general_search`). `match: true` is substring matching. Search with a customer name or radio handle if the operator convention uses that value, then post-filter returned account names exactly after normalization. For repeatable follow-up, search by the returned account ID using an integer field; the ID variable's GraphQL type is `Int64Bit!` on this tenant.

```graphql
query CustomerSearch($term: String!) {
  accounts(
    search: [{ string_fields: [{ attribute: "name", search_value: $term, match: true }] }]
    paginator: { page: 1, records_per_page: 20 }
  ) {
    entities { id name account_status { id name activates_account } }
    page_info { total_count page records_per_page }
  }
}
```

After identifying the account, request account status, service rows, package assignment, and inventory forward through `account_services → inventory_items`; paginate all connections when `page_info.total_count` exceeds the returned rows. The query proves a radio association only if an exact `NAME` is found in the account-linked item fields. `Service.enabled` describes the shared catalog definition, not an individual account assignment, and the verified `AccountService` type has no distinct activation/status field. Preserve active/unknown unless the operator's billing convention confirms assigned-service state. This schema does not yet prove whether `AccountService` or `package` can be treated as a current subscriber indicator.

```graphql
query CustomerDetails($accountId: Int64Bit!) {
  accounts(
    search: [{ integer_fields: [{ attribute: "id", operator: EQ, search_value: $accountId }] }]
    paginator: { page: 1, records_per_page: 2 }
  ) {
    entities {
      id
      name
      account_status { id name activates_account }
      account_services(paginator: { page: 1, records_per_page: 50 }) {
        entities {
          id
          service { name enabled type }
          package { id name }
          inventory_items(paginator: { page: 1, records_per_page: 50 }) {
            entities { id status overall_status inventory_model { name } }
            page_info { total_count page records_per_page }
          }
        }
        page_info { total_count page records_per_page }
      }
    }
    page_info { total_count page records_per_page }
  }
}
```

For name-field discovery, `inventory_model_field_data.value` is searchable and `inventory_model_field_data` returns `value` + `inventory_model_field { name }`. The connection does **not** expose a reverse `inventory_item` field in this tenant schema. Inspect field rows through the account/service/item forward path above. Accept a radio-name relationship only if an exact normalized `NAME` field is present on an inventory item reached from the exact account. Multiple matches, no link, missing status, or pagination not completed means customer↔radio status is unresolved; do not infer inactivity.

Authenticated live validation: account-name search for one radio handle returned one substring candidate but its account name was not an exact normalized match; a second handle returned zero. Following the candidate by account ID returned an account status that did not activate, two enabled catalog service definitions, no linked package records, and zero linked inventory items. This proves neither radio ownership nor subscriber inactivity. Additional account-service search fields `package { id name }` and nested inventory-field connections execute, but no billing convention has been supplied to interpret a package assignment as current/live service. Searching inventory field values for a `<site> LAPGPS` radio name returned one exact `NAME` record, but the record had no account-service link; searching accounts for `<site>` returned zero. The `accounts.search` + ID follow-up schema is verified; the business relationship needs a known operator-confirmed account/radio sample and the correct forward inventory path. Do not claim zero subscribers from these results.

### What the status fields mean

| Field | Interpretation | Not sufficient by itself |
|---|---|---|
| `account.account_status.activates_account` | Whether the named account status is configured to activate the account (tenant field) | Does not prove a specific radio belongs to the account |
| `account_service.service.enabled` | Whether the shared catalog service definition is enabled | Does not prove an account assignment is currently active |
| `account_service.package` | Package assigned to the account-service record | No current/active meaning is assumed without the operator's billing convention |
| `inventory_item.status` | Inventory asset lifecycle (`FUNCTIONAL`, `FAILED`, `LOST`, `SOLD`) | Not billing/service activity |
| `inventory_item.overall_status` | Device monitoring status (`GOOD`, `DOWN`, `WARNING`) | Not customer service status |

Keep queries read-only, request only needed fields, minimize customer PII in output, and never infer an active AP subscriber count solely from inventory/account lookup.

## Working Query - Open NOC Tickets

**Note:** "Open" means any status that is NOT `CLOSED`. Currently the active statuses are `OPEN` and `PENDING_INTERNAL`. `SearchStringField` does not support `NOT_EQ`, so use multiple `string_fields` with `match: true` for each desired status:

```graphql
query {
  tickets(search: {
    integer_fields: [
      { attribute: "ticket_group_id", operator: EQ, search_value: <noc_group_id> }
    ]
    string_fields: [
      { attribute: "status", search_value: "OPEN", match: true },
      { attribute: "status", search_value: "PENDING_INTERNAL", match: true }
    ]
  }) {
    entities {
      id
      subject
      status
      priority
      ticket_group { id name }
      ticket_categories { entities { id name } }
      user { id name }
      created_at
      updated_at
      description
    }
    page_info { total_count page total_pages records_per_page }
  }
}
```

If new statuses are added (e.g. `PENDING_EXTERNAL`, `IN_PROGRESS`), add them to the `string_fields` array.

## Working Query - Search All Tickets by Site Name

Search ALL tickets (all groups, all statuses) for a site name substring. **Omit `integer_fields`** to skip the group filter. Search both `subject` and `description` for comprehensive results:

```graphql
query {
  tickets(search: {
    string_fields: [
      { attribute: "subject", search_value: "<site_name>", match: true },
      { attribute: "description", search_value: "<site_name>", match: true }
    ]
  }, sorter: { attribute: "created_at", direction: DESC }, paginator: { page: 1, records_per_page: 100 }) {
    entities {
      id
      subject
      status
      priority
      ticket_categories { entities { id name } }
      ticket_group { id name }
      created_at
      updated_at
      description
    }
    page_info { total_count page total_pages records_per_page }
  }
}
```

### Notes

- `match: true` does **substring matching** (contains, not exact). Searching for `Kilo` returns tickets for `Kilo`, `LKilo`, `Kilo Farms`, etc. — post-filter as needed.
- Default page size is 25. Set `records_per_page: 100` for larger result sets.
- Sort by `created_at DESC` for most recent first, or `ASC` for oldest first.
- **Cannot filter by `ticket_category_id`** — it is not a valid search field. Filter categories in code after fetching.
- To search only NOC tickets by site name, add the group filter back: `integer_fields: [{ attribute: "ticket_group_id", operator: EQ, search_value: <noc_group_id> }]`

### Example: Search for a Site Across All Groups

```graphql
query {
  tickets(search: {
    string_fields: [
      { attribute: "subject", search_value: "LKilo", match: true },
      { attribute: "description", search_value: "LKilo", match: true }
    ]
  }, sorter: { attribute: "created_at", direction: DESC }) {
    entities { id subject status ticket_categories { entities { id name } } }
    page_info { total_count }
  }
}
```

### Example: Search NOC Tickets for a Site

```graphql
query {
  tickets(search: {
    integer_fields: [
      { attribute: "ticket_group_id", operator: EQ, search_value: <noc_group_id> }
    ]
    string_fields: [
      { attribute: "subject", search_value: "MKilo", match: true }
    ]
  }) {
    entities { id subject status }
    page_info { total_count }
  }
}
```

## Working Query - Ticket Comments (fetch first comment for site identification)

```graphql
query {
  ticket_comments(search: {
    integer_fields: [
      { attribute: "ticket_id", operator: EQ, search_value: <ticket_id> }
    ]
  }, sorter: { attribute: "created_at", direction: ASC }, paginator: { page: 1, records_per_page: 1 }) {
    entities {
      id
      body
      user { name }
      created_at
    }
  }
}
```

## Working Mutation - Post Comment to Ticket

```graphql
mutation {
  createTicketComment(input: {
    ticket_id: <ticket_id>
    body: "<HTML content>"
  }) {
    id
  }
}
```

**CRITICAL: Always verify the mutation response.** The response must contain a comment `id` — if it returns `null` or an error, the comment was NOT posted. Do NOT report success without checking the response.

Example of a successful response:
```json
{"data":{"createTicketComment":{"id":"12345"}}}
```

Example of a failed response (missing `id` or `errors` array):
```json
{"data":{"createTicketComment":null},"errors":[{"message":"..."}]}
```

## Working Mutation - Update Ticket (Group, Status, Categories)

The `updateTicket` mutation takes `id` as a separate argument (not inside `input`):

```graphql
mutation {
  updateTicket(id: <ticket_id>, input: {
    ticket_group_id: <group_id>
    status: <STATUS_ENUM>
    ticket_category_ids: [<cat_id_1>, <cat_id_2>, ...]
  }) {
    id
    status
    ticket_group { id name }
    ticket_categories { entities { id name } }
  }
}
```

**Important:** `ticket_category_ids` replaces ALL categories, not adds to it. Include all desired category IDs.

### Status Enum Values

- `OPEN` — Active, open ticket
- `CLOSED` — Resolved/closed
- `PENDING_INTERNAL` — Waiting on internal team
- `PENDING_EXTERNAL` — Waiting on customer/external party

### Example: Reassign Ticket to CS Level 2

```graphql
mutation {
  updateTicket(id: <ticket_id>, input: {
    ticket_group_id: <cs_level_2_id>
  }) {
    id
    ticket_group { id name }
  }
}
```

### Example: Add Category While Preserving Existing

First fetch current categories, then include all IDs in the mutation:

```graphql
mutation {
  updateTicket(id: <ticket_id>, input: {
    ticket_category_ids: [<existing_cat_id>, <slow_speeds_id>, <weather_id>]
  }) {
    id
    ticket_categories { entities { id name } }
  }
}
```

## curl Template

```bash
curl -s -X POST 'https://your-org.sonar.software/api/graphql' \
  -H "Authorization: Bearer $SONAR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"<graphql query>"}'
```

## Posting Comments — Use the Helper (Required)

**Canonical:** `scripts/sonar_comment.py` — handles escaping and verification. See `comment-formatting` for the `BODYPLACEHOLDER` rule.

```bash
cat > /tmp/comment.html << 'HTMLEOF'
<p><span style=&quot;color:green&quot;>→ ✓ ARM RB4011, healthy</span></p>
HTMLEOF
python3 scripts/sonar_comment.py 12345 /tmp/comment.html --verify
```

**Why &quot;?** GraphQL string inside JSON: raw `"` needs `\\\"` → `\"` → `"`. Using `&quot;` avoids any `"` in HTML, so single `json.dumps` suffices. See `comment-formatting` skill for full explanation.

**Forbidden:** Never use a `BODYPLACEHOLDER` template + `replace('"BODYPLACEHOLDER"', json.dumps(html))` hack — it fails because `json.dumps` escapes to `\\"BODYPLACEHOLDER\\"`.

**Verification (required):** After posting, fetch back `ticket_comments` and assert body contains expected marker and not `BODYPLACEHOLDER` (helper does this with `--verify`).

### Legacy Methods (Deprecated)

**Method 1 - Single-quoted outer JSON (fragile, keep for reference only):**
```bash
-d '{"query":"mutation { createTicketComment(input: { ticket_id: 12345, body: \"<p><span style=\\\"color:red\\\">text</span></p>\" }) { id } }"}'
```

**Method 2 - Inline Python with `&quot;` (use helper instead):**
```python
import json, subprocess, os
token = subprocess.run(['bash', '-c', 'source .env && echo $SONAR_TOKEN'], capture_output=True, text=True).stdout.strip()
html = '<p><span style=&quot;color:green&quot;>→ ✓ ARM RB4011, healthy</span></p>'
mutation = f'mutation {{ createTicketComment(input: {{ ticket_id: 12345, body: "{html}" }}) {{ id }} }}'
query = json.dumps({"query": mutation})
result = subprocess.run(['curl', '-s', '-X', 'POST', 'https://your-org.sonar.software/api/graphql',
    '-H', f'Authorization: Bearer {token}', '-H', 'Content-Type: application/json', '-d', query],
    capture_output=True, text=True)
```

**If the colored spans don't render in Sonar**, the escaping is wrong. Fetch the posted comment back and check if `style="color:..."` is intact vs `style=\\\"color:...\\\"` (double-escaped = broken).
