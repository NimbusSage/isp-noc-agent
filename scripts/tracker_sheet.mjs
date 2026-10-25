#!/usr/bin/env node
// Tracker Sheet helper — NOC Ticket Tracker 2.1 append-only writer.
//
// Companion to the `tracker-sheet` skill. Reads the live tracker tab plus its
// Pick List dropdowns and writes ONE validated row per invocation into the first
// empty row top-down (never overwrites a tech's active input).
//
// Two intents today, both still send via Send Count so N8N picks them up:
//   - Ticket path: create_ticket true/omitted → Create Ticket=TRUE, Send Count=1
//     → the sheet automation creates the real Sonar ticket from the row.
//   - Alert-only path (no ticket): create_ticket false → Create Ticket=FALSE,
//     Send Count=1 → N8N can still send the Google Chat alert, but the sheet
//     automation must NOT create a ticket because Create Ticket is FALSE.
//
// The Create Ticket column (Q, col 16) is what the sheet automation reads to
// decide ticket vs alert-only. The helper default is TRUE, so existing
// "create a ticket" behavior is unchanged; create_ticket:false is the
// alert-only path (typical: grid outages, planned/prearranged power events).
//
// Sending runs through Send Count (N8N watches it), so every write path is
// gated and audited.
//
// Auth: user-OAuth only (tracker sheet lives outside the company domain).
// Reuses scripts/google_user_auth.mjs — same pattern as temp_ospf_sheet.mjs.
// Secrets live outside the repo (owner-only file), never in chat/git.
//
// Commands (read-only unless noted):
//   inspect                          sheet metadata + header signature + dropdowns + row count
//   plan-append --data '<json>'      validate + dup-guard + print planned row (NO write)
//   append --data '<json>' --confirm-write true     gated single-row append + readback + audit
//   export-training [--out file]     labeled JSONL of real rows (future local classifier)
//
// Two intents today, both still send via Send Count so N8N picks them up:
//   - Ticket path: data.create_ticket true/omitted → Create Ticket=TRUE,
//     Send Count=1 → sheet automation creates the real Sonar ticket.
//   - Alert-only path (no ticket): data.create_ticket false → Create Ticket=FALSE,
//     Send Count=1 → N8N can still send Google Chat alert; sheet automation must
//     NOT create a ticket because Create Ticket is FALSE.
//
// Gates for append (ALL required):
//   TRACKER_ACCESS=write, TRACKER_WRITE_ENABLED=true, CLI --confirm-write true
//
// Live schema (verified against the org's tracker spreadsheet):
//   Tracker tab 'NOC Ticket Tracker' (default name; TRACKER_TAB overrides), header row 1, 46+ cols A:AT+.
//   Col D (idx 3) is 'Distance (miles)' — 'n/a' for single-site, auto-filled
//   miles for link tickets. Agent fills A:S (0-19); T:AT auto-fill (coords,
//   contacts, ticket name). Ticket # (F) filled by automation.
//   Status: 'Pick List'!J11:J21 (10 values, J18 blank). Issue: A2:A44 (43).
//   Priority: L11:L14. Tower1/2: Pick List I2:I. Booleans: G,K,M,N,Q,S.
import { appendFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadEnvPrefixes } from './env_load.mjs';
import {
  GOOGLE_SHEETS,
  GOOGLE_SHEETS_READONLY,
  getGoogleUserToken,
} from './google_user_auth.mjs';

const SHEETS_API = 'https://sheets.googleapis.com/v4/spreadsheets';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function auditFile() {
  return process.env.TRACKER_AUDIT_FILE
    ? resolve(process.env.TRACKER_AUDIT_FILE)
    : resolve(ROOT, 'logs/tracker_sheet_audit.jsonl');
}

// Header signature: anchor columns the helper depends on (index -> exact text).
const HEADER_SIGNATURE = {
  0: 'Status',
  1: 'Tower 1 (AP)',
  2: 'Tower 2 (ST)',
  3: 'Distance (miles)',
  4: 'Town Name, and problem device',
  5: 'Ticket #',
  6: 'Issue',
  15: 'Tech Who Reached Out to TH',
  16: 'Notes',
  17: 'Create Ticket',
  18: 'Priority',
  19: 'Send to Auto Tracker',
};

const RESOLVED_STATUS = 'Resolved';
// A duplicate = same towers + issue with a non-resolved status (still live).
function isLiveRow(status) {
  return String(status || '').trim() !== '' && String(status).trim() !== RESOLVED_STATUS;
}

export function loadConfig(env = process.env) {
  const spreadsheetId = env.TRACKER_SPREADSHEET_ID;
  if (!spreadsheetId) throw new Error('Missing TRACKER_SPREADSHEET_ID; set the tracker spreadsheet id (from its URL) in .env');
  const tab = env.TRACKER_TAB || 'NOC Ticket Tracker';
  const pickTab = env.TRACKER_PICK_TAB || 'Pick List';
  const authMode = env.TRACKER_AUTH_MODE || 'user-oauth';
  if (authMode !== 'user-oauth') throw new Error('TRACKER_AUTH_MODE must be user-oauth (external-domain sheet)');
  const oauthFile = env.TRACKER_OAUTH_FILE;
  if (!oauthFile) throw new Error('Missing TRACKER_OAUTH_FILE; configure the user-OAuth secret path in .env');
  return {
    spreadsheetId,
    tab,
    pickTab,
    authMode,
    oauthFile: resolve(oauthFile),
    access: env.TRACKER_ACCESS || 'readonly',
    writeEnabled: env.TRACKER_WRITE_ENABLED === 'true',
    headerRow: Number(env.TRACKER_HEADER_ROW || 1),
  };
}

async function tokenFor(cfg, scope, fetchImpl = fetch) {
  return getGoogleUserToken(cfg.oauthFile, scope, fetchImpl);
}

export async function apiGet(cfg, path, scope, fetchImpl = fetch) {
  const token = await tokenFor(cfg, scope, fetchImpl);
  const res = await fetchImpl(`${SHEETS_API}/${cfg.spreadsheetId}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) throw new Error(`Sheets API GET ${path} failed (HTTP ${res.status})`);
  return res.json();
}

export async function readValues(cfg, range, fetchImpl = fetch) {
  const body = await apiGet(cfg, `/values/${encodeURIComponent(range)}`, GOOGLE_SHEETS_READONLY, fetchImpl);
  if (body.error) throw new Error(`Sheets values error for ${range}: ${JSON.stringify(body.error).slice(0, 200)}`);
  return body.values || [];
}

function col(values, i) {
  return (values || []).map((r) => (r[i] ?? '')).filter((v) => String(v).trim() !== '');
}

export async function fetchDropdowns(cfg, fetchImpl = fetch) {
  const [statusRaw, issueRaw, priRaw, towersRaw] = await Promise.all([
    readValues(cfg, `'${cfg.pickTab}'!J11:J21`, fetchImpl),
    readValues(cfg, `'${cfg.pickTab}'!A2:A44`, fetchImpl),
    readValues(cfg, `'${cfg.pickTab}'!L11:L14`, fetchImpl),
    readValues(cfg, `'${cfg.pickTab}'!I2:I4000`, fetchImpl),
  ]);
  const statuses = statusRaw.map((r) => r[0] ?? '').filter((v) => String(v).trim() !== '');
  const issues = issueRaw.map((r) => r[0] ?? '').filter((v) => String(v).trim() !== '');
  const priorities = priRaw.map((r) => r[0] ?? '').filter((v) => String(v).trim() !== '');
  const towers = new Set(towersRaw.map((r) => String(r[0] ?? '').trim()).filter(Boolean));
  return { statuses, issues, priorities, towers };
}

export function checkHeaderSignature(headerRow) {
  const mismatches = [];
  for (const [idx, expected] of Object.entries(HEADER_SIGNATURE)) {
    const actual = headerRow[Number(idx)] ?? '';
    if (actual !== expected) mismatches.push({ col: Number(idx), expected, actual });
  }
  return mismatches;
}

export function validateTicket(data, dropdowns) {
  const errors = [];
  const req = (v, name) => {
    if (!String(v ?? '').trim()) errors.push(`${name} is required`);
  };
  req(data.status, 'status');
  req(data.tower1, 'tower1');
  req(data.issue, 'issue');
  req(data.notes, 'notes');
  if (data.status && !dropdowns.statuses.includes(data.status)) {
    errors.push(`status ${JSON.stringify(data.status)} not in Pick List J11:J21`);
  }
  if (data.issue && !dropdowns.issues.includes(data.issue)) {
    errors.push(`issue ${JSON.stringify(data.issue)} not in Pick List A2:A44`);
  }
  const priority = data.priority || 'LOW';
  if (!dropdowns.priorities.includes(priority)) {
    errors.push(`priority ${JSON.stringify(priority)} not in Pick List L11:L14`);
  }
  if (data.tower1 && !dropdowns.towers.has(String(data.tower1).trim())) {
    errors.push(`tower1 ${JSON.stringify(data.tower1)} not in tower list`);
  }
  if (data.tower2 && String(data.tower2).trim() && !dropdowns.towers.has(String(data.tower2).trim())) {
    errors.push(`tower2 ${JSON.stringify(data.tower2)} not in tower list`);
  }
  if (data.status === RESOLVED_STATUS || data.status === 'Dispatched') {
    errors.push(`status ${JSON.stringify(data.status)} is a lifecycle state; new tickets must not use it`);
  }
  // Ticket # (F) is normally owned by the sheet automation on the ticket path.
  // An explicit Ticket # is only allowed on alert-only rows — the natural
  // high-confidence-duplicate workflow references the EXISTING ticket the
  // duplicate belongs to (create_ticket:false + ticket). On the ticket path
  // a hand-written Ticket # would fight the automation.
  if (data.ticket && String(data.ticket).trim() && data.create_ticket !== false) {
    errors.push(`ticket (Ticket #) may only be set on alert-only rows (create_ticket:false); the automation owns F on the ticket path`);
  }
  return { errors, priority, tech: String(data.tech || 'NOC Agent').trim() || 'NOC Agent' };
}

const bool = (v) => (String(v ?? '').toUpperCase() === 'TRUE' ? 'TRUE' : 'FALSE');

// Build the 21-cell A:U row. Ticket # (F) stays blank for the automation on
// the ticket path; alert-only duplicate rows (create_ticket:false) may carry
// data.ticket so the row points at the EXISTING ticket it belongs to.
// Distance (D, idx 3) is a sheet ARRAYFORMULA (auto-calculates miles between
// the two sites, 'n/a' for single-site) — the helper NEVER writes it: idx 3
// is always '' here and the writer skips column D entirely. Writing any value
// into D breaks the spill and takes down D1 with #REF! (seen live).
//
// Two intents today, both still send via Send Count so N8N picks them up:
//
//  - Ticket path: create_ticket true/omitted + Create Ticket=TRUE + Send Count=1
//    → the sheet automation creates the real Sonar ticket from the row.
//  - Alert-only path (no ticket): create_ticket false + Create Ticket=FALSE +
//    Send Count=1 → N8N can still send the Google Chat alert, but the sheet
//    automation must NOT create a ticket because Create Ticket is FALSE.
//
// create_ticket is the agent-facing intent flag; Create Ticket is the sheet
// column the automation reads. Helper default (omitted create_ticket) is true
// so existing "create a ticket" behavior is unchanged.
//
// S (Send to Auto Tracker checkbox) stays FALSE — the API path sends via
// T (Send Count) instead: N8N watches T for changes, so a new ticket writes
// T=1. Resends/modifies increment the existing T value (never the checkbox).
export function buildRow(data, priority, tech) {
  const send = data.send === undefined ? true : String(data.send).toUpperCase() === 'TRUE';
  const createTicket = data.create_ticket === false
    ? 'FALSE'
    : 'TRUE';
  return [
    data.status, // A Status
    data.tower1, // B Tower 1 (AP)
    data.tower2 || '', // C Tower 2 (ST)
    '', // D Distance (miles) — FORMULA column, never written (see above)
    data.town_device || '', // E Town Name, and problem device
    createTicket === 'TRUE' ? '' : String(data.ticket || ''), // F Ticket # (automation fills on ticket path; alert-only dup may reference existing)
    data.issue, // G Issue
    bool(data.is_cx_ap), // H is cx AP
    data.ap_ip || '', // I AP IP
    data.tracking_ip || '', // J Tracking IP
    data.towers_affected || '', // K Towers Affected
    bool(data.called), // L Called TH
    data.last_called || '', // M Last Time Called (h:MM AM/PM)
    bool(data.texted), // N Texted TH
    bool(data.oncall_dispatched), // O On-Call Dispatched
    tech, // P Tech Who Reached Out to TH
    data.notes, // Q Notes
    createTicket, // R Create Ticket — TRUE=ticket request, FALSE=alert only (no ticket)
    priority, // S Priority
    'FALSE', // T Send to Auto Tracker (human path; API sends via U)
    send ? '1' : '', // U Send Count (N8N trigger; resends increment it)
  ];
}

// A row is open when every human-filled cell is blank. Checkbox/boolean
// columns (G,K,M,N,Q,S) come pre-filled FALSE and T:AT holds template
// formulas, so they are ignored — only content cells count. Rows fill
// top-down like a tech.
const CONTENT_CELLS = [0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 15, 16, 18, 20];
export function isRowEmpty(row) {
  return CONTENT_CELLS.every((i) => String(row[i] ?? '').trim() === '');
}

export function firstEmptyRow(rows, { startRow = 2 } = {}) {
  for (let i = 0; i < rows.length; i++) {
    if (isRowEmpty(rows[i])) return startRow + i;
  }
  return -1;
}

export async function findDuplicate(cfg, data, fetchImpl = fetch) {
  const rows = await readValues(cfg, `'${cfg.tab}'!A2:R1000`, fetchImpl);
  const t1 = String(data.tower1 || '').trim().toLowerCase();
  const t2 = String(data.tower2 || '').trim().toLowerCase();
  const iss = String(data.issue || '').trim().toLowerCase();
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    const rs = String(r[0] ?? '').trim();
    if (!isLiveRow(rs)) continue;
    if (
      String(r[1] ?? '').trim().toLowerCase() === t1 &&
      String(r[2] ?? '').trim().toLowerCase() === t2 &&
      String(r[6] ?? '').trim().toLowerCase() === iss
    ) {
      return { row: i + 2, status: rs, ticket: String(r[5] ?? '').trim() };
    }
  }
  return null;
}

export async function planAppend(cfg, data, fetchImpl = fetch) {
  const headerRows = await readValues(cfg, `'${cfg.tab}'!A1:AT1`, fetchImpl);
  const mismatches = checkHeaderSignature(headerRows[0] || []);
  if (mismatches.length) {
    throw new Error(`Header signature drift — refusing to write: ${JSON.stringify(mismatches)}`);
  }
  const dropdowns = await fetchDropdowns(cfg, fetchImpl);
  const { errors, priority, tech } = validateTicket(data, dropdowns);
  if (errors.length) throw new Error(`Ticket validation failed: ${errors.join('; ')}`);
  // Duplicate guard — natural high-confidence-duplicate workflow (operator rule):
  // same towers + same issue against a live row is a high-confidence
  // duplicate; a SECOND ticket must never be created for it. The
  // ticket path (create_ticket true/omitted) refuses. The alert-only path
  // (create_ticket:false + explicit ticket referencing the EXISTING ticket)
  // is allowed so the company alert still fires with the row pointing at the
  // existing ticket. Slightly different issues/towers are never caught here —
  // only exact tower1+tower2+issue matches count as duplicates.
  const duplicate = await findDuplicate(cfg, data, fetchImpl);
  if (duplicate && data.create_ticket !== false) {
    throw new Error(
      `Duplicate live row ${duplicate.row} (status ${duplicate.status}, ticket ${duplicate.ticket || 'none yet'}) — refusing second ticket. High-confidence duplicate: reuse the existing ticket ${duplicate.ticket || '(see row)'}, append new info to it, and file the alert as an alert-only row (create_ticket:false + ticket:${duplicate.ticket || '<existing #>'})`,
    );
  }
  const grid = await readValues(cfg, `'${cfg.tab}'!A2:U1000`, fetchImpl);
  const targetRow = firstEmptyRow(grid);
  if (targetRow < 0) throw new Error('No empty row in A2:U1000 — sheet is full, escalate to a human');
  return { row: buildRow(data, priority, tech), priority, tech, targetRow, duplicate };
}

function audit(entry) {
  const file = auditFile();
  mkdirSync(dirname(file), { recursive: true });
  appendFileSync(file, `${JSON.stringify(entry)}\n`);
}

export async function appendTicket(cfg, data, { confirmWrite = false } = {}, fetchImpl = fetch) {
  if (cfg.access !== 'write') throw new Error('TRACKER_ACCESS is not write; escalate to Editor grant + ACCESS=write first');
  if (!cfg.writeEnabled) throw new Error('TRACKER_WRITE_ENABLED is not true; write kill switch is off');
  if (!confirmWrite) throw new Error('Append requires --confirm-write true (deliberate CLI gate)');
  // Prewrite re-read: dup guard + target row still empty (a tech may have
  // taken it since planning — never overwrite someone's active input).
  const planned = await planAppend(cfg, data, fetchImpl);
  const fresh = await readValues(cfg, `'${cfg.tab}'!A${planned.targetRow}:U${planned.targetRow}`, fetchImpl);
  if (!isRowEmpty(fresh[0] || [])) {
    throw new Error(`Target row ${planned.targetRow} is no longer empty — someone is inputting there; re-run plan-append for a fresh row`);
  }
  // Column D holds a sheet ARRAYFORMULA and must never be written, so the
  // row goes out as TWO ranges (A:C + E:U) in one atomic batchUpdate.
  const token = await tokenFor(cfg, GOOGLE_SHEETS, fetchImpl);
  const res = await fetchImpl(
    `${SHEETS_API}/${cfg.spreadsheetId}/values:batchUpdate`,
    {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'content-type': 'application/json' },
      body: JSON.stringify({
        valueInputOption: 'USER_ENTERED',
        data: [
          { range: `'${cfg.tab}'!A${planned.targetRow}:C${planned.targetRow}`, values: [planned.row.slice(0, 3)] },
          { range: `'${cfg.tab}'!E${planned.targetRow}:U${planned.targetRow}`, values: [planned.row.slice(4)] },
        ],
      }),
      signal: AbortSignal.timeout(30_000),
    },
  );
  if (!res.ok) throw new Error(`Sheets row write failed (HTTP ${res.status})`);
  // Readback verify the written row.
  const readback = await readValues(cfg, `'${cfg.tab}'!A${planned.targetRow}:U${planned.targetRow}`, fetchImpl);
  const written = readback[0] || [];
  for (const [idx, name] of [[0, 'Status'], [1, 'Tower1'], [5, 'Ticket #'], [6, 'Issue'], [16, 'Notes'], [17, 'CreateTicket'], [20, 'SendCount']]) {
    if (String(written[idx] ?? '') !== String(planned.row[idx])) {
      throw new Error(`Readback mismatch on ${name}: wrote ${JSON.stringify(planned.row[idx])}, read ${JSON.stringify(written[idx])}`);
    }
  }
  const entry = {
    ts: new Date().toISOString(),
    sheet_row: planned.targetRow,
    status: planned.row[0],
    tower1: planned.row[1],
    tower2: planned.row[2],
    ticket: planned.row[5] || '',
    issue: planned.row[6],
    priority: planned.row[18],
    tech: planned.row[15],
    send_count: planned.row[20] || '',
  };
  // High-confidence-duplicate alert-only write — record what it duplicates so
  // the audit trail shows the row is a follow-up alert on an existing ticket.
  if (planned.duplicate) {
    entry.duplicate_alert_only = true;
    entry.duplicate_row = planned.duplicate.row;
    entry.duplicate_ticket = planned.duplicate.ticket || '';
  }
  audit(entry);
  return { ok: true, ...entry, audit_file: 'logs/tracker_sheet_audit.jsonl' };
}

const defaultSleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function parseRowArg(v) {
  const n = Number(v);
  if (!v || !Number.isInteger(n) || n <= 0) throw new Error('wait-for-ticket requires --row <positive integer>');
  return n;
}

export function parseMsArg(v, flag) {
  const n = Number(v);
  if (!v || !Number.isInteger(n) || n <= 0) throw new Error(`wait-for-ticket requires --${flag} <positive integer ms>`);
  return n;
}

export async function waitForTicket(cfg, row, { intervalMs = 60000, timeoutMs = 600000, sleepImpl = defaultSleep } = {}, fetchImpl = fetch) {
  const headerRows = await readValues(cfg, `'${cfg.tab}'!A1:AT1`, fetchImpl);
  const mismatches = checkHeaderSignature(headerRows[0] || []);
  if (mismatches.length) {
    throw new Error(`Header signature drift — refusing to wait: ${JSON.stringify(mismatches)}`);
  }
  const rowVals = await readValues(cfg, `'${cfg.tab}'!A${row}:R${row}`, fetchImpl);
  const cells = rowVals[0] || [];
  if (String(cells[17] ?? '').trim().toUpperCase() !== 'TRUE') {
    throw new Error(`tracker_sheet: row ${row} is not a created ticket (Create Ticket is not TRUE) — refusing to wait`);
  }
  const start = Date.now();
  for (;;) {
    const eVals = await readValues(cfg, `'${cfg.tab}'!F${row}`, fetchImpl);
    const ticket = String(eVals?.[0]?.[0] ?? '').trim();
    if (ticket) return { ok: true, sheet_row: row, ticket };
    const waitedS = Math.round((Date.now() - start) / 1000);
    if (Date.now() - start >= timeoutMs) {
      audit({ ts: new Date().toISOString(), sheet_row: row, waited_s: waitedS, result: 'timeout' });
      throw new Error(`tracker_sheet: no Ticket # in F${row} after ${Math.max(1, waitedS)}s`);
    }
    await sleepImpl(intervalMs);
  }
}

export async function inspectSheet(cfg, fetchImpl = fetch) {
  const meta = await apiGet(
    cfg,
    '?fields=properties.title,sheets.properties',
    GOOGLE_SHEETS_READONLY,
    fetchImpl,
  );
  const tabs = (meta.sheets || []).map((s) => ({
    id: s.properties.sheetId,
    title: s.properties.title,
    rows: s.properties.gridProperties?.rowCount,
    cols: s.properties.gridProperties?.columnCount,
  }));
  const headerRows = await readValues(cfg, `'${cfg.tab}'!A1:AT1`, fetchImpl);
  const mismatches = checkHeaderSignature(headerRows[0] || []);
  const dropdowns = await fetchDropdowns(cfg, fetchImpl);
  const dataRows = await readValues(cfg, `'${cfg.tab}'!A2:A1000`, fetchImpl);
  // Count live (non-blank status or issue) rows without pulling everything.
  const af = await readValues(cfg, `'${cfg.tab}'!A2:G1000`, fetchImpl);
  const live = af.filter((r) => String(r[0] ?? '').trim() || String(r[6] ?? '').trim()).length;
  return {
    title: meta.properties?.title,
    tabs,
    header_columns: (headerRows[0] || []).length,
    header_signature: mismatches.length ? { ok: false, mismatches } : { ok: true },
    statuses: dropdowns.statuses,
    issue_count: dropdowns.issues.length,
    priorities: dropdowns.priorities,
    tower_count: dropdowns.towers.size,
    data_rows_scanned: dataRows.length,
    live_rows: live,
  };
}

export async function exportTraining(cfg, outFile, fetchImpl = fetch) {
  const rows = await readValues(cfg, `'${cfg.tab}'!A2:R1000`, fetchImpl);
  const lines = [];
  for (const r of rows) {
    const g = (i) => String(r[i] ?? '').trim();
    if (!g(0) && !g(6)) continue;
    lines.push(
      JSON.stringify({
        status: g(0),
        tower1: g(1),
        tower2: g(2),
        town_device: g(4),
        issue: g(6),
        called: g(11).toUpperCase() === 'TRUE',
        texted: g(13).toUpperCase() === 'TRUE',
        tech: g(15),
        notes: g(16),
        priority: g(18),
      }),
    );
  }
  const out = outFile || resolve(ROOT, 'evals/golden/tracker_training.jsonl');
  const { writeFileSync } = await import('node:fs');
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, `${lines.join('\n')}\n`);
  return { ok: true, rows: lines.length, out };
}

function parseArgs(argv) {
  const out = { command: argv[2] || '', data: null, confirmWrite: false, outFile: '', row: '', intervalMs: '', timeoutMs: '' };
  for (let i = 3; i < argv.length; i++) {
    if (argv[i] === '--data') out.data = JSON.parse(argv[++i]);
    else if (argv[i] === '--confirm-write') out.confirmWrite = String(argv[++i]).toLowerCase() === 'true';
    else if (argv[i] === '--out') out.outFile = argv[++i];
    else if (argv[i] === '--row') out.row = argv[++i];
    else if (argv[i] === '--interval-ms') out.intervalMs = argv[++i];
    else if (argv[i] === '--timeout-ms') out.timeoutMs = argv[++i];
  }
  return out;
}

async function main() {
  loadEnvPrefixes(['TRACKER_']);   // only the tracker settings; the rest of .env is not read into the environment
  const args = parseArgs(process.argv);
  const cfg = loadConfig();
  if (args.command === 'inspect') {
    console.log(JSON.stringify(await inspectSheet(cfg), null, 2));
  } else if (args.command === 'plan-append') {
    if (!args.data) throw new Error('plan-append requires --data \'{...}\'');
    const planned = await planAppend(cfg, args.data);
    console.log(JSON.stringify({ target_row: planned.targetRow, planned_row_A_T: planned.row, priority: planned.priority, tech: planned.tech, duplicate: planned.duplicate || null }, null, 2));
  } else if (args.command === 'append') {
    if (!args.data) throw new Error('append requires --data \'{...}\'');
    console.log(JSON.stringify(await appendTicket(cfg, args.data, { confirmWrite: args.confirmWrite }), null, 2));
  } else if (args.command === 'wait-for-ticket') {
    const row = parseRowArg(args.row);
    const opts = {};
    if (args.intervalMs) opts.intervalMs = parseMsArg(args.intervalMs, 'interval-ms');
    if (args.timeoutMs) opts.timeoutMs = parseMsArg(args.timeoutMs, 'timeout-ms');
    console.log(JSON.stringify(await waitForTicket(cfg, row, opts)));
  } else if (args.command === 'export-training') {
    console.log(JSON.stringify(await exportTraining(cfg, args.outFile || ''), null, 2));
  } else {
    throw new Error('Usage: node scripts/tracker_sheet.mjs inspect | plan-append --data \'{...}\' | append --data \'{...}\' --confirm-write true | wait-for-ticket --row <n> [--interval-ms <ms>] [--timeout-ms <ms>] | export-training [--out file]');
  }
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  main().catch((err) => {
    console.error(`tracker_sheet: ${err.message}`);
    process.exit(1);
  });
}
