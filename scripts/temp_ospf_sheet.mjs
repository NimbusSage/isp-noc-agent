#!/usr/bin/env node
import { createHash, createSign } from 'node:crypto';
import { appendFileSync, closeSync, mkdirSync, openSync, readFileSync, realpathSync, statSync, unlinkSync } from 'node:fs';
import { dirname, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const TOKEN_URL = 'https://oauth2.googleapis.com/token';
const SHEETS_API = 'https://sheets.googleapis.com/v4/spreadsheets';
const READ_SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly';
const WRITE_SCOPE = 'https://www.googleapis.com/auth/spreadsheets';
const MAX_READ_ROWS = 10_000;
const MAX_COLUMNS = 52; // A:AZ
const tokenCache = new Map();

function loadProjectEnv(file = resolve(ROOT, '.env')) {
  let text;
  try { text = readFileSync(file, 'utf8'); } catch { return; }
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!match || Object.hasOwn(process.env, match[1])) continue;
    let value = match[2];
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
      if (match[2].startsWith('"')) {
        try { value = JSON.parse(match[2]); } catch { /* retain unwrapped value */ }
      }
    } else {
      value = value.replace(/\s+#.*$/, '');
    }
    process.env[match[1]] = value;
  }
}

function requiredEnv(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing ${name}; configure it in the project .env`);
  return value;
}

function config() {
  const access = process.env.TEMP_OSPF_ACCESS?.trim().toLowerCase() || 'readonly';
  if (!['readonly', 'write'].includes(access)) throw new Error('TEMP_OSPF_ACCESS must be readonly or write');
  const authMode = process.env.TEMP_OSPF_AUTH_MODE?.trim().toLowerCase() || 'service-account';
  if (!['service-account', 'user-oauth'].includes(authMode)) throw new Error('TEMP_OSPF_AUTH_MODE must be service-account or user-oauth');
  const readRowsText = process.env.TEMP_OSPF_READ_ROWS || '40';
  const headerRowText = process.env.TEMP_OSPF_HEADER_ROW || '0';
  if (!/^\d+$/.test(readRowsText) || !/^\d+$/.test(headerRowText)) throw new Error('TEMP_OSPF_READ_ROWS and TEMP_OSPF_HEADER_ROW must be whole numbers');
  const readRows = Number(readRowsText);
  const headerRow = Number(headerRowText);
  if (readRows < 1 || readRows > MAX_READ_ROWS) throw new Error(`TEMP_OSPF_READ_ROWS must be 1-${MAX_READ_ROWS}`);
  if (headerRow < 0 || headerRow > MAX_READ_ROWS) throw new Error(`TEMP_OSPF_HEADER_ROW must be unset for inspect or 1-${MAX_READ_ROWS}`);
  return {
    spreadsheetId: requiredEnv('TEMP_OSPF_SPREADSHEET_ID'),
    tab: process.env.TEMP_OSPF_TAB?.trim() || 'Temp OSPF',
    authMode,
    credentialFile: authMode === 'service-account'
      ? resolve(requiredEnv('TEMP_OSPF_CREDENTIALS_FILE'))
      : (process.env.TEMP_OSPF_CREDENTIALS_FILE?.trim() ? resolve(process.env.TEMP_OSPF_CREDENTIALS_FILE.trim()) : null),
    oauthFile: authMode === 'user-oauth' ? resolve(requiredEnv('TEMP_OSPF_OAUTH_FILE')) : null,
    access,
    readRows,
    headerRow,
    inUseValue: parseInUseValue(process.env.TEMP_OSPF_IN_USE_VALUE),
  };
}

function readServiceAccount(file) {
  const resolvedFile = realpathSync(file);
  const resolvedRoot = realpathSync(ROOT);
  const rel = relative(resolvedRoot, resolvedFile);
  if (rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`))) {
    throw new Error('Google credential JSON must be outside the repository; .env should contain its path only');
  }
  const stats = statSync(resolvedFile);
  if (!stats.isFile()) throw new Error('Credential path must be a regular file');
  if (process.platform !== 'win32' && (stats.mode & 0o077) !== 0) {
    throw new Error('Credential file permissions are too broad; use an owner-only file (chmod 600)');
  }
  const account = JSON.parse(readFileSync(resolvedFile, 'utf8'));
  if (account.type !== 'service_account' || !account.client_email || !account.private_key) {
    throw new Error('Credential JSON must be a Google service-account key file');
  }
  if (account.token_uri && account.token_uri !== TOKEN_URL) throw new Error('Only Google OAuth token endpoint is allowed');
  return account;
}

import {
  buildUserConsentUrl as buildConsentUrl,
  exchangeAuthCode as exchangeCode,
  getGoogleUserToken,
  readOAuthSecrets,
  storeRefreshToken,
} from './google_user_auth.mjs';

// Re-exported for existing callers/tests; the implementation lives in the
// shared google_user_auth module so future skills reuse the same grant.
export { buildConsentUrl as buildUserConsentUrl, exchangeCode as exchangeAuthCode };

export function makeServiceAssertion(account, scope, nowSeconds = Math.floor(Date.now() / 1000)) {
  function base64url(value) { return Buffer.from(value).toString('base64url'); }
  const header = base64url(JSON.stringify({ alg: 'RS256', typ: 'JWT' }));
  const payload = base64url(JSON.stringify({
    iss: account.client_email,
    scope,
    aud: account.token_uri || TOKEN_URL,
    iat: nowSeconds,
    exp: nowSeconds + 3600,
  }));
  const unsigned = `${header}.${payload}`;
  const signer = createSign('RSA-SHA256');
  signer.update(unsigned);
  signer.end();
  return `${unsigned}.${signer.sign(account.private_key).toString('base64url')}`;
}

async function accessToken(account, scope, fetchImpl = fetch) {
  const cacheKey = `${account.client_email}:${scope}`;
  const cached = tokenCache.get(cacheKey);
  if (cached && cached.expiresAt > Date.now() + 60_000) return cached.token;
  const assertion = makeServiceAssertion(account, scope);
  const response = await fetchImpl(account.token_uri || TOKEN_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer', assertion }),
    signal: AbortSignal.timeout(20_000),
  });
  if (!response.ok) throw new Error(`Google OAuth token request failed (HTTP ${response.status}); verify key, Sheets API enablement, and scope`);
  const body = await response.json();
  const expiresIn = Number(body.expires_in);
  if (typeof body.access_token !== 'string' || !body.access_token || !Number.isFinite(expiresIn) || expiresIn <= 0) {
    throw new Error('Google OAuth response omitted a valid access token/expiry');
  }
  tokenCache.set(cacheKey, { token: body.access_token, expiresAt: Date.now() + expiresIn * 1000 });
  return body.access_token;
}

async function googleFetch(url, token, options = {}, fetchImpl = fetch) {
  const response = await fetchImpl(url, {
    ...options,
    headers: { authorization: `Bearer ${token}`, ...(options.headers || {}) },
    signal: options.signal || AbortSignal.timeout(20_000),
  });
  if (!response.ok) {
    const status = response.status;
    const reason = status === 403 ? 'service account lacks spreadsheet access or requested OAuth scope' : status === 404 ? 'spreadsheet or tab was not found' : 'Google Sheets API request failed';
    throw new Error(`${reason} (HTTP ${status})`);
  }
  return response.json();
}

function quoteTab(tab) { return `'${tab.replaceAll("'", "''")}'`; }

export function columnLabel(zeroBased) {
  if (!Number.isInteger(zeroBased) || zeroBased < 0 || zeroBased >= MAX_COLUMNS) throw new Error('Temp OSPF sheet column is outside supported A:AZ range');
  let n = zeroBased + 1;
  let label = '';
  while (n > 0) {
    const remainder = (n - 1) % 26;
    label = String.fromCharCode(65 + remainder) + label;
    n = Math.floor((n - 1) / 26);
  }
  return label;
}

export function validatePrivate30(cidr) {
  const match = String(cidr).trim().match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/30$/);
  if (!match) throw new Error('Subnet must be an IPv4 /30 CIDR');
  const octets = match.slice(1).map(Number);
  if (octets.some(value => value > 255)) throw new Error('Subnet contains an invalid IPv4 octet');
  const network = octets.reduce((acc, value) => ((acc << 8) | value) >>> 0, 0);
  if ((network & 3) !== 0) throw new Error('CIDR is not the aligned network address for a /30');
  const [first, second] = octets;
  if (!(first === 10 || (first === 172 && second >= 16 && second <= 31) || (first === 192 && second === 168))) {
    throw new Error('Temporary OSPF /30 must be in RFC1918 private space');
  }
  return `${octets.join('.')}/30`;
}

function parseInUseValue(value) {
  if (value === undefined || value.trim() === '') return true;
  if (value.trim().toLowerCase() === 'true') return true;
  throw new Error('TEMP_OSPF_IN_USE_VALUE must be true for a checkbox; false/text values are not allowed');
}

export function findHeaderIndex(headerRow, configuredHeader) {
  if (!configuredHeader?.trim()) throw new Error('Required header mapping is missing; inspect the real tab and set the exact header name in .env');
  const matches = [];
  headerRow.forEach((value, index) => {
    if (String(value ?? '').trim() === configuredHeader.trim()) matches.push(index);
  });
  if (matches.length !== 1) throw new Error(`Configured header must match exactly one sheet column: ${configuredHeader}`);
  return matches[0];
}

function cell(tab, column, row) { return `${quoteTab(tab)}!${columnLabel(column)}${row}`; }

async function getMetadata(cfg, token, fetchImpl) {
  const url = `${SHEETS_API}/${encodeURIComponent(cfg.spreadsheetId)}?fields=properties(title),sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)))`;
  return googleFetch(url, token, {}, fetchImpl);
}

async function getValues(cfg, token, range, fetchImpl) {
  const url = `${SHEETS_API}/${encodeURIComponent(cfg.spreadsheetId)}/values/${encodeURIComponent(range)}?valueRenderOption=FORMATTED_VALUE`;
  return googleFetch(url, token, {}, fetchImpl);
}

async function authorize(cfg, writing, fetchImpl) {
  if (writing && (cfg.access !== 'write' || process.env.TEMP_OSPF_WRITE_ENABLED !== 'true')) {
    throw new Error('Writes disabled; require TEMP_OSPF_ACCESS=write, TEMP_OSPF_WRITE_ENABLED=true, and explicit --confirm-write after read-only schema review');
  }
  const scope = writing ? WRITE_SCOPE : READ_SCOPE;
  if (cfg.authMode === 'user-oauth') {
    if (!cfg.oauthFile) throw new Error('Missing TEMP_OSPF_OAUTH_FILE; configure the user-OAuth secret path in .env');
    return { token: await getGoogleUserToken(cfg.oauthFile, scope, fetchImpl), authMode: 'user-oauth' };
  }
  const account = readServiceAccount(cfg.credentialFile);
  return { token: await accessToken(account, scope, fetchImpl), authMode: 'service-account' };
}

export async function inspectSheet(cfg = config(), fetchImpl = fetch) {
  const { token } = await authorize(cfg, false, fetchImpl);
  const info = await getMetadata(cfg, token, fetchImpl);
  const sheet = (info.sheets || []).find(entry => entry.properties?.title === cfg.tab);
  if (!sheet) throw new Error(`Configured Temp OSPF tab not found; available tabs: ${(info.sheets || []).map(entry => entry.properties?.title).join(', ') || '(none)'}`);
  const columns = Math.min(Number(sheet.properties.gridProperties?.columnCount || MAX_COLUMNS), MAX_COLUMNS);
  const rows = Math.min(cfg.readRows, Number(sheet.properties.gridProperties?.rowCount || cfg.readRows));
  if (!Number.isInteger(columns) || columns < 1 || !Number.isInteger(rows) || rows < 1) throw new Error('Sheet tab has no readable rows or columns');
  const range = `${quoteTab(cfg.tab)}!A1:${columnLabel(columns - 1)}${rows}`;
  const values = await getValues(cfg, token, range, fetchImpl);
  return {
    spreadsheetTitle: info.properties?.title || '(untitled)',
    tab: cfg.tab,
    tabId: sheet.properties.sheetId,
    grid: { rows: sheet.properties.gridProperties?.rowCount, columns: sheet.properties.gridProperties?.columnCount },
    inspectedRange: range,
    values: values.values || [],
    note: 'Bounded read-only preview. Identify the actual header row, subnet, in-use checkbox, and both site headers before enabling writes.',
  };
}

function acquireLocalLock(spreadsheetId) {
  const file = resolve('/tmp', `temp-ospf-${createHash('sha256').update(spreadsheetId).digest('hex').slice(0, 24)}.lock`);
  let fd;
  try {
    fd = openSync(file, 'wx', 0o600);
    appendFileSync(file, `${process.pid}\n`);
  } catch (error) {
    if (fd !== undefined) { try { closeSync(fd); } catch {} try { unlinkSync(file); } catch {} }
    if (error?.code === 'EEXIST') throw new Error('Another local Temp OSPF writer holds the lock; inspect it before clearing any stale lock');
    throw error;
  }
  return () => { try { closeSync(fd); } finally { try { unlinkSync(file); } catch {} } };
}

function isChecked(value) { return ['true', 'yes', 'y', '1', '☑', '☒'].includes(String(value ?? '').trim().toLowerCase()); }
function isUnchecked(value) { return ['', 'false', 'no', 'n', '0', '☐'].includes(String(value ?? '').trim().toLowerCase()); }

function appendAudit(event) {
  const file = resolve(process.env.TEMP_OSPF_AUDIT_FILE || resolve(ROOT, 'logs', 'temp-ospf-sheet.jsonl'));
  mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
  appendFileSync(file, `${JSON.stringify(event)}\n`, { mode: 0o600 });
}

function auditContext(incidentId, subnet, rows, siteA, siteB) {
  return { action: 'mark-in-use', incidentId, subnet, rows, siteA, siteB };
}

/** Usable host dotted-strings for an aligned /30 network CIDR. */
export function pairedHosts(cidr) {
  const network = validatePrivate30(cidr);
  const base = network.split('/')[0].split('.').map(Number)
    .reduce((acc, value) => ((acc << 8) | value) >>> 0, 0);
  const dotted = (n) => [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join('.');
  return { network, hostA: dotted(base + 1), hostB: dotted(base + 2) };
}

/**
 * Live Temp OSPF layout (verified live): paired single-host rows.
 * Header row holds `/30` over the host-IP column, `Router name` (the per-row
 * router/site column, previously unlabeled), `Which radios are
 * used`, `In use:`, `Date installed:`, `reason`, and `date re-opened`
 * (added later; the owner stamps it when a reservation is released).
 * Columns are derived from the `/30` anchor and the rest of the signature is
 * verified — any human schema change fails closed instead of mis-writing.
 */
export function resolvePairedColumns(headers) {
  const anchors = [];
  headers.forEach((value, index) => {
    if (String(value ?? '').trim() === '/30') anchors.push(index);
  });
  if (anchors.length !== 1) throw new Error(`Expected exactly one '/30' header column; found ${anchors.length} — schema changed, human review required`);
  const h = anchors[0];
  const at = (i) => String(headers[i] ?? '').trim();
  const problems = [];
  if (at(h + 1) !== 'Router name') problems.push(`expected 'Router name' at ${columnLabel(h + 1)}`);
  if (at(h + 2) !== 'Which radios are used') problems.push(`expected 'Which radios are used' at ${columnLabel(h + 2)}`);
  if (at(h + 3) !== 'In use:') problems.push(`expected 'In use:' at ${columnLabel(h + 3)}`);
  if (at(h + 5) !== 'reason') problems.push(`expected 'reason' at ${columnLabel(h + 5)}`);
  if (at(h + 6) !== 'date re-opened') problems.push(`expected 'date re-opened' at ${columnLabel(h + 6)}`);
  if (problems.length) throw new Error(`Temp OSPF header signature mismatch: ${problems.join('; ')}`);
  return { host: h, site: h + 1, inUse: h + 3, reason: h + 5, reopened: h + 6 };
}

async function readTabMeta(cfg, token, fetchImpl) {
  const info = await getMetadata(cfg, token, fetchImpl);
  const sheet = (info.sheets || []).find(entry => entry.properties?.title === cfg.tab);
  if (!sheet) throw new Error(`Configured Temp OSPF tab not found: ${cfg.tab}`);
  const rowCount = Number(sheet.properties.gridProperties?.rowCount || 0);
  const columnCount = Number(sheet.properties.gridProperties?.columnCount || 0);
  if (columnCount > MAX_COLUMNS) throw new Error(`Tab has ${columnCount} columns; writer only supports A:AZ, refusing schema truncation`);
  if (!Number.isInteger(cfg.headerRow) || cfg.headerRow < 1 || cfg.headerRow > rowCount) throw new Error('Set TEMP_OSPF_HEADER_ROW to the inspected one-based header row (live: 1)');
  if (rowCount > MAX_READ_ROWS) throw new Error(`Tab has ${rowCount} rows, above safe writer limit ${MAX_READ_ROWS}; review and narrow before writing`);
  if (rowCount <= cfg.headerRow) throw new Error('No allocation rows exist below the configured header row');
  return { rowCount, columnCount, lastColumn: columnLabel(columnCount - 1) };
}

function validateReservationInputs(subnet, siteA, siteB, incidentId) {
  const { network, hostA, hostB } = pairedHosts(subnet);
  if (!siteA?.trim() || !siteB?.trim() || !incidentId?.trim()) throw new Error('siteA, siteB, and incidentId are required');
  if (siteA.trim() === siteB.trim()) throw new Error('siteA and siteB must identify two distinct sites');
  if (incidentId.trim().length > 120) throw new Error('incidentId must be 120 characters or fewer');
  return { network, hostA, hostB };
}

async function locatePairedRows(cfg, token, fetchImpl, network, hostA, hostB) {
  const { rowCount, lastColumn } = await readTabMeta(cfg, token, fetchImpl);
  const headerRange = `${quoteTab(cfg.tab)}!A${cfg.headerRow}:${lastColumn}${cfg.headerRow}`;
  const headers = (await getValues(cfg, token, headerRange, fetchImpl)).values?.[0] || [];
  const cols = resolvePairedColumns(headers);
  const scanFirstRow = cfg.headerRow + 1;
  const hostRange = `${quoteTab(cfg.tab)}!${columnLabel(cols.host)}${scanFirstRow}:${columnLabel(cols.host)}${rowCount}`;
  const hostRows = (await getValues(cfg, token, hostRange, fetchImpl)).values || [];
  const matchesA = [];
  const matchesB = [];
  hostRows.forEach((row, index) => {
    const value = String(row?.[0] ?? '').trim();
    if (value === hostA) matchesA.push(scanFirstRow + index);
    if (value === hostB) matchesB.push(scanFirstRow + index);
  });
  if (matchesA.length !== 1 || matchesB.length !== 1) {
    throw new Error(`Expected exactly one row each for ${hostA} + ${hostB} below header; found ${matchesA.length} + ${matchesB.length}`);
  }
  const [rowA] = matchesA;
  const [rowB] = matchesB;
  if (rowB !== rowA + 1) throw new Error(`Host rows ${hostA} (row ${rowA}) and ${hostB} (row ${rowB}) are not a consecutive pair; human review required`);
  return { cols, rowA, rowB, rowCount, lastColumn };
}

async function readPairRows(cfg, token, fetchImpl, cols, rowA, rowB, lastColumn) {
  const rangeA = `${quoteTab(cfg.tab)}!A${rowA}:${lastColumn}${rowA}`;
  const rangeB = `${quoteTab(cfg.tab)}!A${rowB}:${lastColumn}${rowB}`;
  const [rowAValues, rowBValues] = await Promise.all([
    getValues(cfg, token, rangeA, fetchImpl).then(r => r.values?.[0] || []),
    getValues(cfg, token, rangeB, fetchImpl).then(r => r.values?.[0] || []),
  ]);
  return { rowAValues, rowBValues };
}

function pairState(cols, hostA, hostB, siteA, siteB, incidentId, rowAValues, rowBValues) {
  const cell = (row, col) => String(row[col] ?? '').trim();
  const hostOk = cell(rowAValues, cols.host) === hostA && cell(rowBValues, cols.host) === hostB;
  const siteOk = cell(rowAValues, cols.site) === siteA && cell(rowBValues, cols.site) === siteB;
  const reasonOk = cell(rowAValues, cols.reason) === incidentId && cell(rowBValues, cols.reason) === incidentId;
  const checkedOk = isChecked(rowAValues[cols.inUse]) && isChecked(rowBValues[cols.inUse]);
  // The sheet's own "available" marker is site=open + unchecked (seen live
  // ); a truly blank site counts the same. Anything else is occupied.
  const siteFree = (row, col) => ['', 'open'].includes(cell(row, col).toLowerCase());
  const freeOk = siteFree(rowAValues, cols.site) && siteFree(rowBValues, cols.site) &&
    isUnchecked(rowAValues[cols.inUse]) && isUnchecked(rowBValues[cols.inUse]) &&
    !cell(rowAValues, cols.reason) && !cell(rowBValues, cols.reason);
  return { hostOk, siteOk, reasonOk, checkedOk, freeOk };
}

/** Read-only dry-run: locate + validate the pair, report exactly what a write would change. */
export async function planReservation({ subnet, siteA, siteB, incidentId }, cfg = config(), fetchImpl = fetch) {
  const { network, hostA, hostB } = validateReservationInputs(subnet, siteA, siteB, incidentId);
  const { token } = await authorize(cfg, false, fetchImpl);
  const { cols, rowA, rowB } = await locatePairedRows(cfg, token, fetchImpl, network, hostA, hostB);
  const { rowAValues, rowBValues } = await readPairRows(cfg, token, fetchImpl, cols, rowA, rowB, columnLabel(cols.reason));
  const state = pairState(cols, hostA, hostB, siteA.trim(), siteB.trim(), incidentId.trim(), rowAValues, rowBValues);
  if (!state.hostOk) throw new Error('Host cells changed during inspection; refresh and review manually');
  const wouldWrite = [
    { cell: cell(cfg.tab, cols.site, rowA), value: siteA.trim() },
    { cell: cell(cfg.tab, cols.site, rowB), value: siteB.trim() },
    { cell: cell(cfg.tab, cols.inUse, rowA), value: true },
    { cell: cell(cfg.tab, cols.inUse, rowB), value: true },
    { cell: cell(cfg.tab, cols.reason, rowA), value: incidentId.trim() },
    { cell: cell(cfg.tab, cols.reason, rowB), value: incidentId.trim() },
  ];
  if (state.checkedOk && state.siteOk && state.reasonOk) {
    return { status: 'already_in_use_matches', subnet: network, rows: [rowA, rowB], hosts: [hostA, hostB], free: false, verified: true, wouldWrite: [] };
  }
  if (!state.freeOk) throw new Error(`Pair ${hostA}/${hostB} (rows ${rowA}/${rowB}) is partially used or has unrecognized values; human review required — no write planned`);
  return { status: 'would_mark_in_use', subnet: network, rows: [rowA, rowB], hosts: [hostA, hostB], free: true, verified: false, wouldWrite };
}

/** Read-only scan: first N free consecutive host pairs in the live tab. */
export async function findFreePairs(count = 5, cfg = config(), fetchImpl = fetch) {
  const { token } = await authorize(cfg, false, fetchImpl);
  const { rowCount, lastColumn } = await readTabMeta(cfg, token, fetchImpl);
  const headerRange = `${quoteTab(cfg.tab)}!A${cfg.headerRow}:${lastColumn}${cfg.headerRow}`;
  const headers = (await getValues(cfg, token, headerRange, fetchImpl)).values?.[0] || [];
  const cols = resolvePairedColumns(headers);
  const scanFirstRow = cfg.headerRow + 1;
  const hostRange = `${quoteTab(cfg.tab)}!${columnLabel(cols.host)}${scanFirstRow}:${columnLabel(cols.host)}${rowCount}`;
  const hostRows = (await getValues(cfg, token, hostRange, fetchImpl)).values || [];
  const hosts = hostRows.map((row, index) => ({ ip: String(row?.[0] ?? '').trim(), row: scanFirstRow + index }));
  const byIp = new Map(hosts.filter((h) => h.ip).map((h) => [h.ip, h.row]));
  const free = [];
  const seenNets = new Set();
  for (const { ip, row } of hosts) {
    if (!ip) continue; // separator/blank rows between pairs
    if (free.length >= count) break;
    const octets = ip.split('.').map(Number);
    if (octets.length !== 4 || octets.some((o) => !Number.isInteger(o) || o < 0 || o > 255)) continue;
    const hostNum = octets[3];
    if (hostNum % 4 !== 1) continue; // pair starts at .1 of a /30
    const net = `${octets.slice(0, 3).join('.')}.${hostNum - 1}/30`;
    if (seenNets.has(net)) continue;
    seenNets.add(net);
    const second = `${octets.slice(0, 3).join('.')}.${hostNum + 1}`;
    const rowB = byIp.get(second);
    if (rowB !== row + 1) continue;
    const rangeA = `${quoteTab(cfg.tab)}!A${row}:${lastColumn}${row}`;
    const rangeB = `${quoteTab(cfg.tab)}!A${rowB}:${lastColumn}${rowB}`;
    const [a, b] = await Promise.all([
      getValues(cfg, token, rangeA, fetchImpl).then((r) => r.values?.[0] || []),
      getValues(cfg, token, rangeB, fetchImpl).then((r) => r.values?.[0] || []),
    ]);
    const val = (r, c) => String(r[c] ?? '').trim();
    const siteFree = (r) => ['', 'open'].includes(val(r, cols.site).toLowerCase());
    if (siteFree(a) && siteFree(b) && isUnchecked(a[cols.inUse]) && isUnchecked(b[cols.inUse]) && !val(a, cols.reason) && !val(b, cols.reason)) {
      free.push({ subnet: net, rows: [row, rowB], hosts: [ip, second] });
    }
  }
  return { freePairs: free, scannedHosts: hosts.filter((h) => h.ip).length };
}

/** Release a pair after permanent repair. Only releases rows this incident owns. */
export async function releaseSubnet({ subnet, incidentId, confirmWrite }, cfg = config(), fetchImpl = fetch) {
  if (confirmWrite !== true) throw new Error('Explicit confirmWrite: true approval is required for Sheet writes');
  const { network, hostA, hostB } = pairedHosts(subnet);
  if (!incidentId?.trim()) throw new Error('incidentId is required');
  if (incidentId.trim().length > 120) throw new Error('incidentId must be 120 characters or fewer');
  const cleanIncident = incidentId.trim();
  const releaseLock = acquireLocalLock(cfg.spreadsheetId);
  try {
    const { token } = await authorize(cfg, true, fetchImpl);
    const { cols, rowA, rowB } = await locatePairedRows(cfg, token, fetchImpl, network, hostA, hostB);
    const lastColumn = columnLabel(cols.reopened);
    const readem = () => readPairRows(cfg, token, fetchImpl, cols, rowA, rowB, lastColumn);
    const before = await readem();
    const val = (row, col) => String(row[col] ?? '').trim();
    const siteFree = (row) => ['', 'open'].includes(val(row, cols.site).toLowerCase());
    const releasedState = (a, b) =>
      val(a, cols.host) === hostA && val(b, cols.host) === hostB &&
      siteFree(a) && siteFree(b) && isUnchecked(a[cols.inUse]) && isUnchecked(b[cols.inUse]) &&
      !val(a, cols.reason) && !val(b, cols.reason);
    const reopenStamp = new Date().toLocaleDateString('en-US'); // house format, e.g. 10/22/2026
    if (releasedState(before.rowAValues, before.rowBValues)) {
      return { status: 'already_released', subnet: network, rows: [rowA, rowB], hosts: [hostA, hostB], verified: true };
    }
    const owned = val(before.rowAValues, cols.reason) === cleanIncident &&
      val(before.rowBValues, cols.reason) === cleanIncident &&
      isChecked(before.rowAValues[cols.inUse]) && isChecked(before.rowBValues[cols.inUse]);
    if (!owned) {
      throw new Error(`Pair ${hostA}/${hostB} (rows ${rowA}/${rowB}) is not reserved by ${cleanIncident}; refusing to release another incident's rows`);
    }
    const prewrite = await readem();
    const stillOwned = val(prewrite.rowAValues, cols.reason) === cleanIncident &&
      val(prewrite.rowBValues, cols.reason) === cleanIncident &&
      isChecked(prewrite.rowAValues[cols.inUse]) && isChecked(prewrite.rowBValues[cols.inUse]);
    if (!stillOwned) throw new Error('Target rows changed after inspection; no write attempted. Refresh the Sheet and review manually.');

    const updates = [
      { range: cell(cfg.tab, cols.site, rowA), values: [['open']] },
      { range: cell(cfg.tab, cols.site, rowB), values: [['open']] },
      { range: cell(cfg.tab, cols.inUse, rowA), values: [[false]] },
      { range: cell(cfg.tab, cols.inUse, rowB), values: [[false]] },
      { range: cell(cfg.tab, cols.reason, rowA), values: [['']] },
      { range: cell(cfg.tab, cols.reason, rowB), values: [['']] },
      // House convention (owner's tab, observed live): released rows get
      // `date re-opened` stamped so a future reader knows when they freed up.
      { range: cell(cfg.tab, cols.reopened, rowA), values: [[reopenStamp]] },
      { range: cell(cfg.tab, cols.reopened, rowB), values: [[reopenStamp]] },
    ];
    const updateUrl = `${SHEETS_API}/${encodeURIComponent(cfg.spreadsheetId)}/values:batchUpdate`;
    const context = { action: 'release', incidentId: cleanIncident, subnet: network, rows: [rowA, rowB] };
    try {
      appendAudit({ ...context, at: new Date().toISOString(), outcome: 'attempting' });
    } catch (error) {
      throw new Error(`Audit log could not be written; refusing Sheet update (${error.message})`);
    }
    let updateResponse;
    try {
      updateResponse = await googleFetch(updateUrl, token, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        // USER_ENTERED so the re-open stamp parses as a date like the owner's own rows.
        body: JSON.stringify({ valueInputOption: 'USER_ENTERED', data: updates }),
      }, fetchImpl);
    } catch (error) {
      try { appendAudit({ ...context, at: new Date().toISOString(), outcome: 'unknown', error: error.message }); } catch { /* retain original error */ }
      throw new Error(`Sheet write outcome is unknown; inspect rows ${rowA}/${rowB} before retry (${error.message})`);
    }
    let after;
    try {
      after = await readem();
    } catch (error) {
      try { appendAudit({ ...context, at: new Date().toISOString(), outcome: 'unknown', error: error.message }); } catch { /* retain original error */ }
      throw new Error(`Sheet write was acknowledged but readback failed; inspect rows ${rowA}/${rowB} before retry (${error.message})`);
    }
    const verified = releasedState(after.rowAValues, after.rowBValues) &&
      !!val(after.rowAValues, cols.reopened) && !!val(after.rowBValues, cols.reopened);
    const output = { status: verified ? 'released' : 'readback_mismatch', subnet: network, rows: [rowA, rowB], hosts: [hostA, hostB], verified, updatedCells: updateResponse.totalUpdatedCells ?? null };
    try {
      appendAudit({ ...context, at: new Date().toISOString(), outcome: verified ? 'verified' : 'readback_mismatch', updatedCells: updateResponse.totalUpdatedCells ?? null });
    } catch (error) {
      throw new Error(`Sheet readback ${verified ? 'verified' : 'mismatched'} but audit append failed (${error.message})`);
    }
    if (!verified) throw new Error(`Sheet rows ${rowA}/${rowB} did not verify; inspect current state before retry`);
    return output;
  } finally {
    releaseLock();
  }
}

export async function markSubnetInUse({ subnet, siteA, siteB, incidentId, confirmWrite }, cfg = config(), fetchImpl = fetch) {
  if (confirmWrite !== true) throw new Error('Explicit confirmWrite: true approval is required for Sheet writes');
  const { network, hostA, hostB } = validateReservationInputs(subnet, siteA, siteB, incidentId);
  const cleanA = siteA.trim();
  const cleanB = siteB.trim();
  const cleanIncident = incidentId.trim();
  const releaseLock = acquireLocalLock(cfg.spreadsheetId);
  try {
    const { token } = await authorize(cfg, true, fetchImpl);
    const { cols, rowA, rowB } = await locatePairedRows(cfg, token, fetchImpl, network, hostA, hostB);
    const lastColumn = columnLabel(cols.reason);
    const before = await readPairRows(cfg, token, fetchImpl, cols, rowA, rowB, lastColumn);
    const beforeState = pairState(cols, hostA, hostB, cleanA, cleanB, cleanIncident, before.rowAValues, before.rowBValues);
    if (!beforeState.hostOk) throw new Error('Host cells do not match the expected pair; no write attempted');
    if (beforeState.checkedOk && beforeState.siteOk && beforeState.reasonOk) {
      const confirmed = await readPairRows(cfg, token, fetchImpl, cols, rowA, rowB, lastColumn);
      const recheck = pairState(cols, hostA, hostB, cleanA, cleanB, cleanIncident, confirmed.rowAValues, confirmed.rowBValues);
      if (!(recheck.hostOk && recheck.checkedOk && recheck.siteOk && recheck.reasonOk)) {
        throw new Error('Existing reservation changed during confirmation; inspect it manually');
      }
      return { status: 'already_in_use_matches', subnet: network, rows: [rowA, rowB], hosts: [hostA, hostB], verified: true };
    }
    if (!beforeState.freeOk) throw new Error(`Pair ${hostA}/${hostB} (rows ${rowA}/${rowB}) is partially used or has unrecognized values; no write attempted`);

    // Re-read both target rows just before writing to catch edits made after inspection.
    const prewrite = await readPairRows(cfg, token, fetchImpl, cols, rowA, rowB, lastColumn);
    const preState = pairState(cols, hostA, hostB, cleanA, cleanB, cleanIncident, prewrite.rowAValues, prewrite.rowBValues);
    if (!(preState.hostOk && preState.freeOk)) {
      throw new Error('Target rows changed after inspection; no write attempted. Refresh the Sheet and review manually.');
    }

    const updates = [
      { range: cell(cfg.tab, cols.site, rowA), values: [[cleanA]] },
      { range: cell(cfg.tab, cols.site, rowB), values: [[cleanB]] },
      { range: cell(cfg.tab, cols.inUse, rowA), values: [[cfg.inUseValue]] },
      { range: cell(cfg.tab, cols.inUse, rowB), values: [[cfg.inUseValue]] },
      { range: cell(cfg.tab, cols.reason, rowA), values: [[cleanIncident]] },
      { range: cell(cfg.tab, cols.reason, rowB), values: [[cleanIncident]] },
    ];
    const updateUrl = `${SHEETS_API}/${encodeURIComponent(cfg.spreadsheetId)}/values:batchUpdate`;
    const context = auditContext(cleanIncident, network, [rowA, rowB], cleanA, cleanB);
    try {
      appendAudit({ ...context, at: new Date().toISOString(), outcome: 'attempting' });
    } catch (error) {
      throw new Error(`Audit log could not be written; refusing Sheet update (${error.message})`);
    }

    let updateResponse;
    try {
      updateResponse = await googleFetch(updateUrl, token, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ valueInputOption: 'RAW', data: updates }),
      }, fetchImpl);
    } catch (error) {
      try { appendAudit({ ...context, at: new Date().toISOString(), outcome: 'unknown', error: error.message }); } catch { /* retain original error */ }
      throw new Error(`Sheet write outcome is unknown; inspect rows ${rowA}/${rowB} before retry or device change (${error.message})`);
    }

    let after;
    try {
      after = await readPairRows(cfg, token, fetchImpl, cols, rowA, rowB, lastColumn);
    } catch (error) {
      try { appendAudit({ ...context, at: new Date().toISOString(), outcome: 'unknown', error: error.message }); } catch { /* retain original error */ }
      throw new Error(`Sheet write was acknowledged but readback failed; inspect rows ${rowA}/${rowB} before retry or device change (${error.message})`);
    }
    const afterState = pairState(cols, hostA, hostB, cleanA, cleanB, cleanIncident, after.rowAValues, after.rowBValues);
    const verified = afterState.hostOk && afterState.checkedOk && afterState.siteOk && afterState.reasonOk;
    const output = { status: verified ? 'marked_in_use' : 'readback_mismatch', subnet: network, rows: [rowA, rowB], hosts: [hostA, hostB], verified, updatedCells: updateResponse.totalUpdatedCells ?? null };
    try {
      appendAudit({ ...context, at: new Date().toISOString(), outcome: verified ? 'verified' : 'readback_mismatch', updatedCells: updateResponse.totalUpdatedCells ?? null });
    } catch (error) {
      throw new Error(`Sheet readback ${verified ? 'verified' : 'mismatched'} but audit append failed; do not proceed to device changes (${error.message})`);
    }
    if (!verified) throw new Error(`Sheet rows ${rowA}/${rowB} did not verify; inspect current state before retry or device change`);
    return output;
  } finally {
    releaseLock();
  }
}

function parseArgs(argv) {
  const [command, ...rest] = argv;
  const options = {};
  for (let index = 0; index < rest.length; index++) {
    const arg = rest[index];
    if (!arg.startsWith('--')) throw new Error(`Unexpected argument: ${arg}`);
    const [rawKey, inlineValue] = arg.slice(2).split('=', 2);
    if (!['confirm_write', 'subnet', 'site_a', 'site_b', 'incident_id', 'scope', 'code', 'redirect_uri', 'count'].includes(rawKey.replaceAll('-', '_'))) {
      throw new Error(`Unknown option: --${rawKey}`);
    }
    const key = rawKey.replaceAll('-', '_');
    if (Object.hasOwn(options, key)) throw new Error(`Option may only be specified once: --${rawKey}`);
    const value = inlineValue ?? rest[++index];
    if (!value || value.startsWith('--')) throw new Error(`Missing value for --${rawKey}`);
    options[key] = value;
  }
  return { command, options };
}

async function main() {
  loadProjectEnv();
  const { command, options } = parseArgs(process.argv.slice(2));
  if (command === 'inspect') {
    if (Object.keys(options).length) throw new Error('inspect accepts no options');
    console.log(JSON.stringify(await inspectSheet(), null, 2));
    return;
  }
  if (command === 'oauth-url') {
    const cfg = config();
    if (cfg.authMode !== 'user-oauth') throw new Error('Set TEMP_OSPF_AUTH_MODE=user-oauth to use the user-consent flow');
    const secrets = readOAuthSecrets(cfg.oauthFile, { requireRefresh: false });
    const scope = options.scope === 'read' ? READ_SCOPE : WRITE_SCOPE;
    const redirectUri = options.redirect_uri || 'http://127.0.0.1:53682/callback';
    console.log(buildConsentUrl({ clientId: secrets.clientId, redirectUri }, scope));
    console.log('No local server listens on that redirect — after Allow the browser tab will fail to connect. That is expected: copy the ?code= value from the hung tab ADDRESS BAR and run oauth-bootstrap --code with it.');
    return;
  }
  if (command === 'oauth-exchange') {
    const cfg = config();
    if (cfg.authMode !== 'user-oauth') throw new Error('Set TEMP_OSPF_AUTH_MODE=user-oauth to use the user-consent flow');
    if (!options.code) throw new Error('Missing required option: --code');
    const redirectUri = options.redirect_uri || 'http://127.0.0.1:53682/callback';
    const secrets = readOAuthSecrets(cfg.oauthFile, { requireRefresh: false });
    const body = await exchangeCode(secrets, options.code, redirectUri);
    console.log(JSON.stringify({ exchanged: true, has_refresh_token: true, scope: body.scope || null, note: 'Store body.refresh_token in TEMP_OSPF_OAUTH_FILE as refresh_token/refreshToken via secure transfer; never paste it into chat.' }, null, 2));
    return;
  }
  if (command === 'oauth-bootstrap') {
    // One-time setup only: exchanges the operator-pasted code AND stores the
    // refresh token directly into TEMP_OSPF_OAUTH_FILE (0600). Prints no secret.
    const cfg = config();
    if (cfg.authMode !== 'user-oauth') throw new Error('Set TEMP_OSPF_AUTH_MODE=user-oauth to use the user-consent flow');
    if (!options.code) throw new Error('Missing required option: --code');
    const redirectUri = options.redirect_uri || 'http://127.0.0.1:53682/callback';
    const secrets = readOAuthSecrets(cfg.oauthFile, { requireRefresh: false });
    const body = await exchangeCode(secrets, options.code, redirectUri);
    storeRefreshToken(cfg.oauthFile, body.refresh_token);
    console.log(JSON.stringify({ bootstrapped: true, oauth_file: '<redacted-path>', scope: body.scope || null, note: 'Refresh token stored owner-only. Delete the auth code from chat history; it is single-use.' }, null, 2));
    return;
  }
  if (command === 'plan-reservation') {
    const required = ['subnet', 'site_a', 'site_b', 'incident_id'];
    const missing = required.filter(name => !options[name]);
    if (missing.length) throw new Error(`Missing required options: ${missing.map(name => `--${name.replaceAll('_', '-')}`).join(', ')}`);
    const result = await planReservation({ subnet: options.subnet, siteA: options.site_a, siteB: options.site_b, incidentId: options.incident_id });
    console.log(JSON.stringify(result, null, 2));
    return;
  }
  if (command === 'release') {
    const required = ['subnet', 'incident_id', 'confirm_write'];
    const missing = required.filter(name => !options[name]);
    if (missing.length) throw new Error(`Missing required options: ${missing.map(name => `--${name.replaceAll('_', '-')}`).join(', ')}`);
    if (options.confirm_write !== 'true') throw new Error('Explicit --confirm-write true is required for Sheet writes');
    const result = await releaseSubnet({ subnet: options.subnet, incidentId: options.incident_id, confirmWrite: true });
    console.log(JSON.stringify(result, null, 2));
    return;
  }
  if (command === 'mark-in-use') {
    const required = ['subnet', 'site_a', 'site_b', 'incident_id', 'confirm_write'];
    const missing = required.filter(name => !options[name]);
    if (missing.length) throw new Error(`Missing required options: ${missing.map(name => `--${name.replaceAll('_', '-')}`).join(', ')}`);
    if (options.confirm_write !== 'true') throw new Error('Explicit --confirm-write true is required for Sheet writes');
    const result = await markSubnetInUse({ subnet: options.subnet, siteA: options.site_a, siteB: options.site_b, incidentId: options.incident_id, confirmWrite: true });
    console.log(JSON.stringify(result, null, 2));
    return;
  }
  if (command === 'find-free') {
    const count = Math.min(Number(options.count || 5), 20) || 5;
    const result = await findFreePairs(count);
    console.log(JSON.stringify(result, null, 2));
    return;
  }
  throw new Error('Usage: node scripts/temp_ospf_sheet.mjs inspect | plan-reservation --subnet <private-/30> --site-a <site> --site-b <site> --incident-id <id> | release --subnet <private-/30> --incident-id <id> --confirm-write true | find-free [--count N] | oauth-url [--scope read|write] | oauth-exchange --code <code> | oauth-bootstrap --code <code> | mark-in-use --subnet <private-/30> --site-a <site> --site-b <site> --incident-id <id> --confirm-write true');
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(`Temp OSPF helper error: ${error.message}`);
    process.exitCode = 1;
  });
}
