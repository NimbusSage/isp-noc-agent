import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, randomUUID } from 'node:crypto';
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  buildUserConsentUrl,
  columnLabel,
  findHeaderIndex,
  inspectSheet,
  makeServiceAssertion,
  markSubnetInUse,
  pairedHosts,
  planReservation,
  releaseSubnet,
  resolvePairedColumns,
  validatePrivate30,
} from './temp_ospf_sheet.mjs';

function response(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}

function decodeRange(url) {
  const parsed = new URL(url);
  const valuesAt = parsed.pathname.indexOf('/values/');
  return valuesAt < 0 ? null : decodeURIComponent(parsed.pathname.slice(valuesAt + '/values/'.length));
}

function fixture() {
  const dir = mkdtempSync(join(tmpdir(), 'temp-ospf-test-'));
  const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const credentialFile = join(dir, 'service-account.json');
  writeFileSync(credentialFile, JSON.stringify({
    type: 'service_account',
    client_email: `${randomUUID()}@example.iam.gserviceaccount.com`,
    private_key: privateKey.export({ type: 'pkcs8', format: 'pem' }),
  }));
  chmodSync(credentialFile, 0o600);
  const auditFile = join(dir, 'audit.jsonl');
  const cfg = {
    spreadsheetId: `test-${randomUUID()}`,
    tab: "Temp OSPF '26",
    credentialFile,
    access: 'write',
    readRows: 40,
    headerRow: 1,
    inUseValue: true,
  };
  return { dir, auditFile, cfg };
}

function metadata(cfg) {
  return {
    properties: { title: 'Test IPAM' },
    sheets: [{ properties: { sheetId: 7, title: cfg.tab, gridProperties: { rowCount: 10, columnCount: 4 } } }],
  };
}

const PAIRED_HEADER = ['', '/30', 'Router name', 'Which radios are used', 'In use:', 'Date installed:', 'reason', 'date re-opened'];

function metadataPaired(cfg, rows = 12) {
  return {
    properties: { title: 'Test IPAM' },
    sheets: [{ properties: { sheetId: 7, title: cfg.tab, gridProperties: { rowCount: rows, columnCount: 8 } } }],
  };
}

/** Live-shaped paired fixture: free pair .1/.2 at rows 3-4, occupied .5/.6 at rows 6-7. */
function pairedHostScan() {
  return [[''], ['10.1.2.1'], ['10.1.2.2'], [''], ['10.1.2.5'], ['10.1.2.6']];
}

function pairedRow(host, site = '', inUse = '', reason = '', reopened = '') {
  return ['', host, site, '', inUse, '', reason, reopened];
}

/** Serve single-row reads from a row-number map; avoids brittle range strings. */
function rowMapFetch(sheets, hostScan, batchHandler) {
  const meta = {
    properties: { title: 'Test IPAM' },
    sheets: [{ properties: { sheetId: 7, title: "Temp OSPF '26", gridProperties: { rowCount: 12, columnCount: 8 } } }],
  };
  return async (url, options = {}) => {
    const urlText = String(url);
    if (urlText.includes('oauth2.googleapis.com/token')) return response({ access_token: `token-${randomUUID()}`, expires_in: 3600 });
    if (urlText.endsWith('/values:batchUpdate')) return batchHandler(JSON.parse(options.body));
    const range = decodeRange(urlText) || '';
    if (range.endsWith('!A1:H1')) return response({ values: [PAIRED_HEADER] });
    if (/!B\d+:B\d+$/.test(range)) return response({ values: hostScan });
    const single = range.match(/!A(\d+):[A-Z]+(\d+)$/);
    if (single && single[1] === single[2] && sheets[Number(single[1])]) {
      return response({ values: [sheets[Number(single[1])]()] });
    }
    return response(meta);
  };
}

function installEnv(cfg, auditFile) {
  const prior = {
    writeEnabled: process.env.TEMP_OSPF_WRITE_ENABLED,
    auditFile: process.env.TEMP_OSPF_AUDIT_FILE,
  };
  process.env.TEMP_OSPF_WRITE_ENABLED = 'true';
  process.env.TEMP_OSPF_AUDIT_FILE = auditFile;
  return () => {
    if (prior.writeEnabled === undefined) delete process.env.TEMP_OSPF_WRITE_ENABLED;
    else process.env.TEMP_OSPF_WRITE_ENABLED = prior.writeEnabled;
    if (prior.auditFile === undefined) delete process.env.TEMP_OSPF_AUDIT_FILE;
    else process.env.TEMP_OSPF_AUDIT_FILE = prior.auditFile;
    rmSync(join(tmpdir(), `temp-ospf-${createHash('sha256').update(cfg.spreadsheetId).digest('hex').slice(0, 24)}.lock`), { force: true });
  };
}

test('validates aligned private /30 networks and rejects unsafe forms', () => {
  assert.equal(validatePrivate30('10.1.2.0/30'), '10.1.2.0/30');
  assert.equal(validatePrivate30('172.16.0.4/30'), '172.16.0.4/30'); // secret-scan:allow(address fixture for private-range validation test)
  assert.equal(validatePrivate30('192.168.1.252/30'), '192.168.1.252/30');
  for (const value of ['10.1.2.1/30', '10.1.2.0/29', '8.8.8.0/30', '10.1.2.256/30']) {
    assert.throws(() => validatePrivate30(value));
  }
});

test('maps supported columns and requires a unique exact header', () => {
  assert.equal(columnLabel(0), 'A');
  assert.equal(columnLabel(25), 'Z');
  assert.equal(columnLabel(26), 'AA');
  assert.equal(columnLabel(51), 'AZ');
  assert.equal(findHeaderIndex(['IP', 'Use'], 'Use'), 1);
  assert.throws(() => findHeaderIndex(['Use', 'Use'], 'Use'), /exactly one/);
  assert.throws(() => columnLabel(52), /A:AZ/);
});

test('inspects bounded tab metadata and values with read-only scope', async t => {
  const { dir, cfg } = fixture();
  const restore = installEnv(cfg, join(dir, 'audit.jsonl'));
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  cfg.readRows = 3;
  const calls = [];
  const fakeFetch = async (url, options) => {
    calls.push({ url: String(url), options });
    if (String(url).includes('oauth2.googleapis.com/token')) {
      const params = new URLSearchParams(options.body);
      const payload = JSON.parse(Buffer.from(params.get('assertion').split('.')[1], 'base64url').toString());
      assert.equal(payload.scope, 'https://www.googleapis.com/auth/spreadsheets.readonly');
      return response({ access_token: 'mock-token', expires_in: 3600 });
    }
    if (String(url).includes('/values/')) return response({ values: [['Subnet', 'In Use'], ['10.1.2.16/30', 'FALSE']] });
    return response(metadata(cfg));
  };
  const result = await inspectSheet(cfg, fakeFetch);
  assert.equal(result.spreadsheetTitle, 'Test IPAM');
  assert.equal(result.tabId, 7);
  assert.equal(result.inspectedRange, "'Temp OSPF ''26'!A1:D3");
  assert.equal(result.values.length, 2);
  assert.equal(calls.length, 3);
  assert.ok(calls.every(call => call.options.method !== 'POST' || String(call.url).includes('oauth2.googleapis.com')));
});

test('derives paired hosts and verifies the live header signature', () => {
  assert.deepEqual(pairedHosts('10.1.2.0/30'), { network: '10.1.2.0/30', hostA: '10.1.2.1', hostB: '10.1.2.2' });
  assert.deepEqual(resolvePairedColumns(PAIRED_HEADER), { host: 1, site: 2, inUse: 4, reason: 6, reopened: 7 });
  // Old unlabeled site column (pre- schema) must fail closed.
  assert.throws(() => resolvePairedColumns(['', '/30', '', 'Which radios are used', 'In use:', 'Date installed:', 'reason', 'date re-opened']), /Router name/);
  assert.throws(() => resolvePairedColumns(['', '/30', 'Router name', 'Which radios are used', 'Used?', 'Date installed:', 'reason', 'date re-opened']), /signature mismatch/);
  assert.throws(() => resolvePairedColumns(['', '/30', 'Router name', 'Which radios are used', 'In use:', 'Date installed:', 'reason', 'x']), /date re-opened/);
  assert.throws(() => resolvePairedColumns(['', '/30', 'Router name', 'Which radios are used', 'In use:', 'Date installed:', 'reason', 'x', '', '/30']), /exactly one/);
});

test('plans a free paired reservation without sending any write', async t => {
  const { dir, cfg } = fixture();
  const restore = installEnv(cfg, join(dir, 'audit.jsonl'));
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  const fakeFetch = rowMapFetch(
    { 3: () => pairedRow('10.1.2.1'), 4: () => pairedRow('10.1.2.2') },
    pairedHostScan(),
    () => { writes++; return response({ totalUpdatedCells: 6 }); },
  );
  const result = await planReservation({ subnet: '10.1.2.0/30', siteA: 'North', siteB: 'South', incidentId: 'INC-42' }, cfg, fakeFetch);
  assert.equal(result.status, 'would_mark_in_use');
  assert.deepEqual(result.rows, [3, 4]);
  assert.equal(result.wouldWrite.length, 6);
  assert.equal(writes, 0);
});

test('marks a free consecutive host pair and verifies both-row readback', async t => {
  const { dir, auditFile, cfg } = fixture();
  const restore = installEnv(cfg, auditFile);
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  let rowA = pairedRow('10.1.2.1');
  let rowB = pairedRow('10.1.2.2');
  const fakeFetch = rowMapFetch(
    { 3: () => rowA, 4: () => rowB },
    pairedHostScan(),
    (request) => {
      writes++;
      assert.equal(request.valueInputOption, 'RAW');
      assert.deepEqual(request.data.map(entry => entry.values[0][0]), ['North', 'South', true, true, 'INC-42', 'INC-42']);
      rowA = pairedRow('10.1.2.1', 'North', 'TRUE', 'INC-42');
      rowB = pairedRow('10.1.2.2', 'South', 'TRUE', 'INC-42');
      return response({ totalUpdatedCells: 6 });
    },
  );
  const result = await markSubnetInUse({ subnet: '10.1.2.0/30', siteA: 'North', siteB: 'South', incidentId: 'INC-42', confirmWrite: true }, cfg, fakeFetch);
  assert.deepEqual(result, { status: 'marked_in_use', subnet: '10.1.2.0/30', rows: [3, 4], hosts: ['10.1.2.1', '10.1.2.2'], verified: true, updatedCells: 6 });
  assert.equal(writes, 1);
  assert.match(await (await import('node:fs/promises')).readFile(auditFile, 'utf8'), /"outcome":"verified"/);
});

test('treats the sheet open marker as a free pair', async t => {
  const { dir, cfg } = fixture();
  const restore = installEnv(cfg, join(dir, 'audit.jsonl'));
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  const fakeFetch = rowMapFetch(
    { 3: () => pairedRow('10.1.2.1', 'open', 'FALSE', ''), 4: () => pairedRow('10.1.2.2', 'OPEN', 'FALSE', '') },
    pairedHostScan(),
    () => { writes++; return response({ totalUpdatedCells: 6 }); },
  );
  const result = await planReservation({ subnet: '10.1.2.0/30', siteA: 'North', siteB: 'South', incidentId: 'INC-50' }, cfg, fakeFetch);
  assert.equal(result.status, 'would_mark_in_use');
  assert.equal(result.free, true);
  assert.equal(writes, 0);
});

test('refuses duplicate host occurrences before sending any write', async t => {
  const { dir, auditFile, cfg } = fixture();
  const restore = installEnv(cfg, auditFile);
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  const fakeFetch = async (url, options = {}) => {
    const urlText = String(url);
    if (urlText.includes('oauth2.googleapis.com/token')) return response({ access_token: `token-${randomUUID()}`, expires_in: 3600 });
    if (urlText.endsWith('/values:batchUpdate')) { writes++; return response({ totalUpdatedCells: 6 }); }
    const range = decodeRange(urlText);
    if (range?.endsWith('!A1:H1')) return response({ values: [PAIRED_HEADER] });
    if (range?.includes('!B2:B12')) return response({ values: [[''], ['10.1.2.1'], ['10.1.2.1'], ['10.1.2.2']] });
    return response(metadataPaired(cfg));
  };
  await assert.rejects(markSubnetInUse({ subnet: '10.1.2.0/30', siteA: 'North', siteB: 'South', incidentId: 'INC-43', confirmWrite: true }, cfg, fakeFetch), /exactly one row each/);
  assert.equal(writes, 0);
});

test('refuses a partially occupied pair and does not treat it as free', async t => {
  const { dir, auditFile, cfg } = fixture();
  const restore = installEnv(cfg, auditFile);
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  const fakeFetch = rowMapFetch(
    { 6: () => pairedRow('10.1.2.5', 'Taken', 'TRUE', 'INC-OLD'), 7: () => pairedRow('10.1.2.6') },
    pairedHostScan(),
    () => { writes++; return response({ totalUpdatedCells: 6 }); },
  );
  await assert.rejects(markSubnetInUse({ subnet: '10.1.2.4/30', siteA: 'North', siteB: 'South', incidentId: 'INC-45', confirmWrite: true }, cfg, fakeFetch), /partially used/);
  assert.equal(writes, 0);
});

test('refuses host rows that are not a consecutive pair', async t => {
  const { dir, auditFile, cfg } = fixture();
  const restore = installEnv(cfg, auditFile);
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  const fakeFetch = rowMapFetch(
    {},
    [[''], ['10.1.2.1'], [''], ['10.1.2.2']],
    () => { writes++; return response({ totalUpdatedCells: 6 }); },
  );
  await assert.rejects(markSubnetInUse({ subnet: '10.1.2.0/30', siteA: 'North', siteB: 'South', incidentId: 'INC-46', confirmWrite: true }, cfg, fakeFetch), /not a consecutive pair/);
  assert.equal(writes, 0);
});

test('releases only rows owned by the incident and verifies the freed pair', async t => {
  const { dir, auditFile, cfg } = fixture();
  const restore = installEnv(cfg, auditFile);
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  const today = new Date().toLocaleDateString('en-US');
  let rowA = pairedRow('10.1.2.1', 'North', 'TRUE', 'INC-42');
  let rowB = pairedRow('10.1.2.2', 'South', 'TRUE', 'INC-42');
  const fakeFetch = rowMapFetch(
    { 3: () => rowA, 4: () => rowB },
    pairedHostScan(),
    (request) => {
      writes++;
      assert.equal(request.valueInputOption, 'USER_ENTERED');
      assert.deepEqual(request.data.map(entry => entry.values[0][0]), ['open', 'open', false, false, '', '', today, today]);
      rowA = pairedRow('10.1.2.1', 'open', 'FALSE', '', today);
      rowB = pairedRow('10.1.2.2', 'open', 'FALSE', '', today);
      return response({ totalUpdatedCells: 8 });
    },
  );
  const result = await releaseSubnet({ subnet: '10.1.2.0/30', incidentId: 'INC-42', confirmWrite: true }, cfg, fakeFetch);
  assert.deepEqual(result, { status: 'released', subnet: '10.1.2.0/30', rows: [3, 4], hosts: ['10.1.2.1', '10.1.2.2'], verified: true, updatedCells: 8 });
  assert.equal(writes, 1);
});

test('a released pair stamped date re-opened still counts as free', async t => {
  const { dir, cfg } = fixture();
  const restore = installEnv(cfg, join(dir, 'audit.jsonl'));
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  const fakeFetch = rowMapFetch(
    { 3: () => pairedRow('10.1.2.1', 'open', 'FALSE', '', '10/20/2026'), 4: () => pairedRow('10.1.2.2', 'open', 'FALSE', '', '10/20/2026') },
    pairedHostScan(),
    () => { writes++; return response({ totalUpdatedCells: 6 }); },
  );
  const result = await planReservation({ subnet: '10.1.2.0/30', siteA: 'North', siteB: 'South', incidentId: 'INC-51' }, cfg, fakeFetch);
  assert.equal(result.status, 'would_mark_in_use');
  assert.equal(result.free, true);
  assert.equal(writes, 0);
});

test('refuses to release rows owned by another incident', async t => {
  const { dir, auditFile, cfg } = fixture();
  const restore = installEnv(cfg, auditFile);
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  const fakeFetch = rowMapFetch(
    { 3: () => pairedRow('10.1.2.1', 'North', 'TRUE', 'INC-OTHER'), 4: () => pairedRow('10.1.2.2', 'South', 'TRUE', 'INC-OTHER') },
    pairedHostScan(),
    () => { writes++; return response({ totalUpdatedCells: 6 }); },
  );
  await assert.rejects(releaseSubnet({ subnet: '10.1.2.0/30', incidentId: 'INC-42', confirmWrite: true }, cfg, fakeFetch), /not reserved by INC-42/);
  assert.equal(writes, 0);
});

test('reports already_released for a free pair without writing', async t => {
  const { dir, auditFile, cfg } = fixture();
  const restore = installEnv(cfg, auditFile);
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  let writes = 0;
  const fakeFetch = rowMapFetch(
    { 3: () => pairedRow('10.1.2.1', 'open', 'FALSE', ''), 4: () => pairedRow('10.1.2.2', 'open', 'FALSE', '') },
    pairedHostScan(),
    () => { writes++; return response({ totalUpdatedCells: 6 }); },
  );
  const result = await releaseSubnet({ subnet: '10.1.2.0/30', incidentId: 'INC-42', confirmWrite: true }, cfg, fakeFetch);
  assert.equal(result.status, 'already_released');
  assert.equal(writes, 0);
});

test('prevents writes unless both environment gates and explicit caller approval are set', async t => {
  const { dir, cfg } = fixture();
  const restore = installEnv(cfg, join(dir, 'audit.jsonl'));
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  delete process.env.TEMP_OSPF_WRITE_ENABLED;
  const fakeFetch = async () => { throw new Error('should not make API calls'); };
  await assert.rejects(markSubnetInUse({ subnet: '10.1.2.0/30', siteA: 'North', siteB: 'South', incidentId: 'INC-44', confirmWrite: true }, cfg, fakeFetch), /Writes disabled/);
});

test('builds a user-consent URL with offline access for the external-domain sheet', () => {
  const url = buildUserConsentUrl({ clientId: 'client-123' }, 'https://www.googleapis.com/auth/spreadsheets.readonly');
  const parsed = new URL(url);
  assert.equal(parsed.hostname, 'accounts.google.com');
  assert.equal(parsed.searchParams.get('client_id'), 'client-123');
  assert.equal(parsed.searchParams.get('access_type'), 'offline');
  assert.equal(parsed.searchParams.get('prompt'), 'consent');
});

test('inspects via user-OAuth refresh grant without a service-account key', async t => {
  const { dir, cfg } = fixture();
  const restore = installEnv(cfg, join(dir, 'audit.jsonl'));
  t.after(() => { restore(); rmSync(dir, { recursive: true, force: true }); });
  cfg.readRows = 3;
  const oauthFile = join(dir, 'oauth.json');
  writeFileSync(oauthFile, JSON.stringify({ client_id: 'client-123', client_secret: 'secret-abc', refresh_token: 'refresh-xyz' }));
  chmodSync(oauthFile, 0o600);
  cfg.authMode = 'user-oauth';
  cfg.oauthFile = oauthFile;
  cfg.credentialFile = null;
  const seen = [];
  const fakeFetch = async (url, options) => {
    seen.push(String(url));
    if (String(url).includes('oauth2.googleapis.com/token')) {
      const params = new URLSearchParams(options.body);
      assert.equal(params.get('grant_type'), 'refresh_token');
      assert.equal(params.get('refresh_token'), 'refresh-xyz');
      return response({ access_token: 'user-token', expires_in: 3600 });
    }
    if (String(url).includes('/values/')) return response({ values: [['Subnet', 'In Use'], ['10.1.2.16/30', 'FALSE']] });
    return response(metadata(cfg));
  };
  const result = await inspectSheet(cfg, fakeFetch);
  assert.equal(result.values.length, 2);
  assert.ok(seen.some(u => u.includes('oauth2.googleapis.com/token')));
});
