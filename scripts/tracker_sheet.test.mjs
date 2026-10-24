#!/usr/bin/env node
// Offline tests for scripts/tracker_sheet.mjs — mocked Sheets fetch, no network.
import test from 'node:test';
import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  appendTicket,
  buildRow,
  checkHeaderSignature,
  findDuplicate,
  firstEmptyRow,
  isRowEmpty,
  parseRowArg,
  parseMsArg,
  planAppend,
  validateTicket,
  waitForTicket,
} from './tracker_sheet.mjs';

const DD = {
  statuses: ['TH Needs Contacted', 'TH Contacted', 'Repair Needs Scheduled', 'Outage Needs Scheduled', 'Dispatched', 'Resolved'],
  issues: ['No Power', 'Link Misaligned', 'No Ether'],
  priorities: ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'],
  towers: new Set(['TownA', 'TowerX', 'SiteD', 'SiteE']),
};

const HEADER = ['Status', 'Tower 1 (AP)', 'Tower 2 (ST)', 'Distance (miles)', 'Town Name, and problem device', 'Ticket #', 'Issue',
  'is cx AP', 'AP IP', 'Tracking IP', 'Towers Affected', 'Called TH', 'Last Time Called', 'Texted TH',
  'On-Call Dispatched', 'Tech Who Reached Out to TH', 'Notes', 'Create Ticket', 'Priority', 'Send to Auto Tracker', 'Send Count'];

const TICKET = {
  status: 'TH Needs Contacted',
  tower1: 'TownA',
  town_device: 'TownA Tower',
  issue: 'No Power',
  tracking_ip: '10.0.2.165',
  notes: 'Site looks like no power - offline since 7:21 PM CT. TH needs contacted in the AM.',
};

// Template blank row as the sheet pre-fills it (FALSE booleans, no content).
const BLANK = ['', '', '', '', '', '', '', 'FALSE', '', '', '', 'FALSE', '', 'FALSE', 'FALSE', '', '', 'FALSE', '', 'FALSE', ''];
const FULL_ROW = ['TH Needs Contacted', 'TownA', '', 'n/a', 'TownA Tower', '', 'No Power', 'FALSE', '', '10.0.2.165', '',
  'FALSE', '', 'FALSE', 'FALSE', 'NOC Agent', 'Site looks like no power.', 'TRUE', 'LOW', 'FALSE', '1'];

// Document the Create Ticket contract in the test file too: R (idx 17) is what
// the sheet automation reads to decide ticket vs alert-only. TRUE = the automation
// creates the real ticket; FALSE = alert-only row (no ticket). Helper default is
// TRUE so existing "create a ticket" behavior is unchanged; create_ticket:false
// is the alert-only path.
const CREATE_TICKET_IDX = 17;
const CREATE_TICKET_TRUE = 'TRUE';
const CREATE_TICKET_FALSE = 'FALSE';

// Mock sheet state: grid rows (A2:U1000) + written single rows.
function mockFetch({ header = HEADER, grid = [FULL_ROW.slice(0, 6), BLANK], afRange = [], single = null, eValues = [] } = {}) {
  const written = new Map();
  const eQueue = [...eValues];
  return async (url, opts = {}) => {
    const u = String(url);
    const json = async (values) => ({ ok: true, json: async () => ({ values }) });
    if (u.includes('/values/') && u.includes('A1')) return json([header]);
    const eCell = u.match(/!F(\d+)$/);
    if (eCell && (opts.method || 'GET') === 'GET') {
      const v = eQueue.length ? eQueue.shift() : '';
      return v === '' ? { ok: true, json: async () => ({}) } : json([[v]]);
    }
    if (u.includes('J11')) return json(DD.statuses.map((s) => [s]));
    if (u.includes('A2%3AA44') || u.includes('A2:A44')) return json(DD.issues.map((s) => [s]));
    if (u.includes('L11')) return json(DD.priorities.map((s) => [s]));
    if (u.includes('I2')) return json([...DD.towers].map((s) => [s]));
    if (u.includes('A2%3AU1000') || u.includes('A2:U1000')) return json(grid);
    if (u.includes('A2%3AR1000') || u.includes('A2:R1000')) return json(afRange);
    const singleMatch = u.match(/A(\d+)%3A[QRU]\1|A(\d+):[QRU]\2/);
    if (singleMatch && (opts.method || 'GET') === 'GET') {
      const n = singleMatch[1] || singleMatch[2];
      if (written.has(n)) return json([written.get(n)]);
      return json(single ? [single] : [[]]);
    }
    if ((opts.method || 'GET') === 'POST' && u.includes('/values:batchUpdate')) {
      const body = JSON.parse(opts.body);
      // Writer skips the formula column D: merges A:C + E:U like the sheet does.
      const full = new Array(21).fill('');
      for (const entry of body.data) {
        const m = entry.range.match(/!([A-Z])(\d+):([A-Z])\d+/);
        const start = m[1].charCodeAt(0) - 65;
        entry.values[0].forEach((v, i) => { full[start + i] = v; });
      }
      const n = body.data[0].range.match(/!(?:[A-Z])(\d+):/)[1];
      assert.ok(!body.data.some((e) => e.range.includes('!D')), 'writer must never touch column D');
      written.set(n, full);
      return { ok: true, json: async () => ({ totalUpdatedCells: 20 }) };
    }
    if (u.includes('oauth2.googleapis.com/token')) {
      return { ok: true, json: async () => ({ access_token: 'mock', expires_in: 3600 }) };
    }
    throw new Error(`unexpected fetch: ${u.slice(0, 160)}`);
  };
}

const CFG = (() => {
  // Owner-only fake OAuth file so the shared auth guard passes offline.
  const dir = mkdtempSync(join(tmpdir(), 'tracker-oauth-'));
  const f = join(dir, 'oauth.json');
  writeFileSync(f, JSON.stringify({ client_id: 'test-client', client_secret: 'test-secret', refresh_token: 'test-refresh' }));
  chmodSync(f, 0o600);
  return {
    spreadsheetId: 'test-id',
    tab: 'NOC Ticket Tracker',
    pickTab: 'Pick List',
    authMode: 'user-oauth',
    oauthFile: f,
    access: 'write',
    writeEnabled: true,
    headerRow: 1,
  };
})();

test('header signature passes on live header', () => {
  assert.deepEqual(checkHeaderSignature(HEADER), []);
});

test('header signature flags drift', () => {
  const bad = [...HEADER];
  bad[6] = 'Problem';
  const m = checkHeaderSignature(bad);
  assert.equal(m.length, 1);
  assert.equal(m[0].col, 6);
});

test('isRowEmpty ignores pre-filled FALSE booleans, catches content', () => {
  assert.equal(isRowEmpty(BLANK), true);
  assert.equal(isRowEmpty([]), true);
  assert.equal(isRowEmpty(['', 'TownA', '', '', '', '']), false);
  assert.equal(isRowEmpty(['TH Contacted', '', '', '', '', 'No Power']), false);
});

test('firstEmptyRow scans top-down', () => {
  assert.equal(firstEmptyRow([FULL_ROW.slice(0, 6), BLANK]), 3);
  assert.equal(firstEmptyRow([BLANK]), 2);
  assert.equal(firstEmptyRow([FULL_ROW.slice(0, 6)]), -1);
});

test('validate accepts a good ticket, defaults LOW + NOC Agent', () => {
  const { errors, priority, tech } = validateTicket(TICKET, DD);
  assert.deepEqual(errors, []);
  assert.equal(priority, 'LOW');
  assert.equal(tech, 'NOC Agent');
});

test('validate rejects bad status/issue/tower', () => {
  const { errors } = validateTicket({ ...TICKET, status: 'Nope', issue: 'Nope', tower1: 'Nope' }, DD);
  assert.equal(errors.length, 3);
});

test('validate rejects Dispatched/Resolved for new tickets', () => {
  for (const s of ['Dispatched', 'Resolved']) {
    const { errors } = validateTicket({ ...TICKET, status: s }, DD);
    assert.ok(errors.some((e) => e.includes('lifecycle')), s);
  }
});

test('buildRow maps the 21 A:U cells, Ticket # blank, R TRUE, T FALSE, U 1', () => {
  const row = buildRow(TICKET, 'LOW', 'NOC Agent');
  assert.equal(row.length, 21);
  assert.equal(row[0], 'TH Needs Contacted');
  assert.equal(row[1], 'TownA');
  assert.equal(row[3], '');
  assert.equal(row[4], 'TownA Tower');
  assert.equal(row[5], '');
  assert.equal(row[6], 'No Power');
  assert.equal(row[9], '10.0.2.165');
  assert.equal(row[15], 'NOC Agent');
  // Create Ticket (R, idx 17) is TRUE by default — that is what creates the
  // real Sonar ticket via the sheet automation, so appending is filing a request.
  assert.equal(row[CREATE_TICKET_IDX], CREATE_TICKET_TRUE);
  assert.equal(row[18], 'LOW');
  assert.equal(row[19], 'FALSE');
  assert.equal(row[20], '1');
});

test('buildRow never writes Distance (sheet ARRAYFORMULA owns column D)', () => {
  for (const t of [TICKET, { ...TICKET, tower2: 'SiteE' }]) {
    assert.equal(buildRow(t, 'LOW', 'NOC Agent')[3], '');
  }
});

test('buildRow honors create_ticket:false (alert-only row, no ticket)', () => {
  const row = buildRow({ ...TICKET, create_ticket: false }, 'LOW', 'NOC Agent');
  assert.equal(row[CREATE_TICKET_IDX], 'FALSE');
  assert.equal(row[18], 'LOW');
  assert.equal(row[20], '1');
});

test('buildRow alert-only still sends (Send Count=1) by default', () => {
  const row = buildRow({ ...TICKET, create_ticket: false }, 'LOW', 'NOC Agent');
  assert.equal(row[20], '1');
});

test('buildRow alert-only + send:false stages without sending', () => {
  const row = buildRow({ ...TICKET, create_ticket: false, send: false }, 'LOW', 'NOC Agent');
  assert.equal(row[CREATE_TICKET_IDX], 'FALSE');
  assert.equal(row[20], '');
});

test('buildRow honors send:false (stage without sending)', () => {
  const row = buildRow({ ...TICKET, send: false }, 'LOW', 'NOC Agent');
  assert.equal(row[19], 'FALSE');
  assert.equal(row[20], '');
});

test('findDuplicate ignores Resolved rows, catches live ones', async () => {
  const af = [
    ['Resolved', 'TownA', '', 'n/a', 'x', '100004', 'No Power'],
    ['TH Contacted', 'TownA', '', 'n/a', 'x', '', 'No Power'],
  ];
  const dup = await findDuplicate(CFG, TICKET, mockFetch({ afRange: af }));
  assert.equal(dup.row, 3);
  const none = await findDuplicate(CFG, TICKET, mockFetch({ afRange: [af[0]] }));
  assert.equal(none, null);
});

test('planAppend targets the first empty row top-down', async () => {
  const p = await planAppend(CFG, TICKET, mockFetch());
  assert.equal(p.targetRow, 3);
  assert.equal(p.row.length, 21);
});

test('planAppend throws on duplicate', async () => {
  const af = [['TH Contacted', 'TownA', '', 'n/a', 'x', '', 'No Power']];
  await assert.rejects(planAppend(CFG, TICKET, mockFetch({ afRange: af })), /Duplicate live row 2/);
});

// High-confidence-duplicate workflow (operator,  <tech-name>): a live
// exact duplicate (same tower1+tower2+issue, status ≠ Resolved) never gets a
// second ticket. The ticket path refuses; the alert-only path is allowed and
// writes the EXISTING ticket # into the Ticket # column (F) so the company
// alert still fires while pointing at the active ticket.
test('validate rejects explicit Ticket # on the ticket path', () => {
  const { errors } = validateTicket({ ...TICKET, ticket: '100002' }, DD);
  assert.ok(errors.some((e) => e.includes('alert-only')));
});

test('buildRow ticket path keeps Ticket # blank even if data.ticket sneaks in', () => {
  const row = buildRow({ ...TICKET, ticket: '100002' }, 'LOW', 'NOC Agent');
  assert.equal(row[5], '');
});

test('planAppend allows alert-only high-confidence duplicate with existing Ticket #', async () => {
  const af = [['Dispatched', 'TownA', '', 'n/a', 'x', '100002', 'No Power']];
  const dup = { ...TICKET, status: 'TH Contacted', create_ticket: false, ticket: '100002' };
  const p = await planAppend(CFG, dup, mockFetch({ afRange: af }));
  assert.ok(p.duplicate);
  assert.equal(p.duplicate.row, 2);
  assert.equal(p.duplicate.ticket, '100002');
  assert.equal(p.row[5], '100002');
  assert.equal(p.row[CREATE_TICKET_IDX], CREATE_TICKET_FALSE);
  assert.equal(p.row[20], '1');
});

test('appendTicket duplicate alert-only write carries dup info in audit + output', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'tracker-dup-'));
  process.env.TRACKER_AUDIT_FILE = join(dir, 'audit.jsonl');
  const dup = { ...TICKET, status: 'TH Contacted', create_ticket: false, ticket: '100002' };
  const out = await appendTicket(CFG, dup, { confirmWrite: true }, mockFetch({ grid: [FULL_ROW.slice(0, 6), BLANK], afRange: [['Dispatched', 'TownA', '', 'n/a', 'x', '100002', 'No Power']] }));
  delete process.env.TRACKER_AUDIT_FILE;
  assert.equal(out.ok, true);
  assert.equal(out.ticket, '100002');
  assert.equal(out.duplicate_alert_only, true);
  assert.equal(out.duplicate_row, 2);
  assert.equal(out.duplicate_ticket, '100002');
  const entry = JSON.parse(readFileSync(join(dir, 'audit.jsonl'), 'utf8').trim());
  assert.equal(entry.duplicate_alert_only, true);
  assert.equal(entry.duplicate_ticket, '100002');
});

test('planAppend throws on header drift', async () => {
  const bad = [...HEADER];
  bad[0] = 'State';
  await assert.rejects(planAppend(CFG, TICKET, mockFetch({ header: bad })), /Header signature drift/);
});

test('appendTicket enforces the three gates', async () => {
  const f = mockFetch();
  await assert.rejects(appendTicket({ ...CFG, access: 'readonly' }, TICKET, { confirmWrite: true }, f), /ACCESS/);
  await assert.rejects(appendTicket({ ...CFG, writeEnabled: false }, TICKET, { confirmWrite: true }, f), /kill switch/);
  await assert.rejects(appendTicket(CFG, TICKET, { confirmWrite: false }, f), /--confirm-write/);
});

test('appendTicket refuses when the target row got taken', async () => {
  const f = mockFetch({ grid: [BLANK], single: FULL_ROW.slice(0, 6) });
  await assert.rejects(appendTicket(CFG, TICKET, { confirmWrite: true }, f), /no longer empty/);
});

test('appendTicket success writes target row, audits, returns row', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'tracker-'));
  process.env.TRACKER_AUDIT_FILE = join(dir, 'audit.jsonl');
  const out = await appendTicket(CFG, TICKET, { confirmWrite: true }, mockFetch({ grid: [FULL_ROW.slice(0, 6), BLANK] }));
  delete process.env.TRACKER_AUDIT_FILE;
  assert.equal(out.ok, true);
  assert.equal(out.sheet_row, 3);
  assert.equal(out.send_count, '1');
  const lines = readFileSync(join(dir, 'audit.jsonl'), 'utf8').trim().split('\n');
  assert.equal(lines.length, 1);
  assert.equal(JSON.parse(lines[0]).tower1, 'TownA');
});

const ROW_Q_TRUE = [...BLANK.slice(0, 17), 'TRUE', ...BLANK.slice(18)];

test('parseRowArg accepts positive ints, rejects junk', () => {
  assert.equal(parseRowArg('784'), 784);
  assert.throws(() => parseRowArg('0'), /--row/);
  assert.throws(() => parseRowArg('-3'), /--row/);
  assert.throws(() => parseRowArg('abc'), /--row/);
  assert.throws(() => parseRowArg(undefined), /--row/);
});

test('parseMsArg validates interval/timeout flags, rejects junk + zero', () => {
  assert.equal(parseMsArg('60000', 'interval-ms'), 60000);
  assert.equal(parseMsArg('600000', 'timeout-ms'), 600000);
  for (const flag of ['interval-ms', 'timeout-ms']) {
    assert.throws(() => parseMsArg('abc', flag), new RegExp(`--${flag}`));
    assert.throws(() => parseMsArg('0', flag), new RegExp(`--${flag}`));
    assert.throws(() => parseMsArg('-5', flag), new RegExp(`--${flag}`));
    assert.throws(() => parseMsArg('1.5', flag), new RegExp(`--${flag}`));
    assert.throws(() => parseMsArg('NaN', flag), new RegExp(`--${flag}`));
    assert.throws(() => parseMsArg('', flag), new RegExp(`--${flag}`));
    assert.throws(() => parseMsArg(undefined, flag), new RegExp(`--${flag}`));
  }
});

test('waitForTicket returns Ticket # present on first poll', async () => {
  const out = await waitForTicket(CFG, 5, { sleepImpl: async () => {} }, mockFetch({ single: ROW_Q_TRUE, eValues: ['100003'] }));
  assert.deepEqual(out, { ok: true, sheet_row: 5, ticket: '100003' });
});

test('waitForTicket refuses when Create Ticket is not TRUE', async () => {
  await assert.rejects(waitForTicket(CFG, 5, {}, mockFetch({ single: BLANK, eValues: ['100003'] })), /not a created ticket/);
});

test('waitForTicket throws on header drift before polling', async () => {
  const bad = [...HEADER];
  bad[0] = 'State';
  await assert.rejects(waitForTicket(CFG, 5, {}, mockFetch({ header: bad, single: ROW_Q_TRUE, eValues: ['100003'] })), /Header signature drift/);
});

test('waitForTicket polls until Ticket # appears (3rd poll)', async () => {
  let polls = 0;
  const f = mockFetch({ single: ROW_Q_TRUE, eValues: ['', '  ', '100003'] });
  const out = await waitForTicket(CFG, 5, { intervalMs: 10, sleepImpl: async () => { polls++; } }, f);
  assert.deepEqual(out, { ok: true, sheet_row: 5, ticket: '100003' });
  assert.equal(polls, 2);
});

test('waitForTicket times out, audits the miss, reports seconds', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'tracker-wait-'));
  process.env.TRACKER_AUDIT_FILE = join(dir, 'audit.jsonl');
  await assert.rejects(
    waitForTicket(CFG, 5, { intervalMs: 5, timeoutMs: 20, sleepImpl: async () => {} }, mockFetch({ single: ROW_Q_TRUE, eValues: [] })),
    /no Ticket # in F5 after \d+s/,
  );
  delete process.env.TRACKER_AUDIT_FILE;
  const lines = readFileSync(join(dir, 'audit.jsonl'), 'utf8').trim().split('\n');
  assert.equal(lines.length, 1);
  assert.deepEqual(JSON.parse(lines[0]), { ts: JSON.parse(lines[0]).ts, sheet_row: 5, waited_s: JSON.parse(lines[0]).waited_s, result: 'timeout' });
});
