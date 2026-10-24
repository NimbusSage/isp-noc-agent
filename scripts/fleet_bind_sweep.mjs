#!/usr/bin/env node
// One-time (or periodic) sweep: pin already-configured pool credentials to
// exact device ID+host bindings so temp-site skills work with zero
// per-incident intervention. Introduces NO new secrets; prints none.
//
//   Dry-run (lists proposed bindings, changes nothing):
//     node scripts/fleet_bind_sweep.mjs --site Alpha
//   Apply:
//     node scripts/fleet_bind_sweep.mjs --site Alpha --apply
//
// Method per candidate: TCP banner grab (no auth) -> dropbear only ->
// single harmless `echo` probe per pool cred in priority order -> bind first
// success. Customer-looking devices (CGNAT/customer subnets, customer-name
// patterns) are ALWAYS skipped. Pool order and concurrency are bounded.
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Socket, isIP } from 'node:net';
import { execFile } from 'node:child_process';
import { defaultStorePath, decryptStore, resolvePassphrase, writeStoreAtomic } from './cred_store.mjs';
import { sshOptionArgs } from './ssh_opts.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const STORE = defaultStorePath(ROOT);
const CACHE = resolve(ROOT, 'logs', 'mymate-device-index.json');

// Customer-looking: never auto-bind these, even if a pool password fits.
// Org-specific extras come from the environment as comma-separated regex
// sources (no commas inside a pattern; use | for alternation), e.g.
//   FLEET_CUSTOMER_SUBNETS='^10\.99\.'   FLEET_CUSTOMER_NAMES='barn|shop'
const envRegexes = (name) => (process.env[name] || '').split(',')
  .map((v) => v.trim()).filter(Boolean).map((v) => new RegExp(v, 'i'));
const CUSTOMER_SUBNETS = [/^100\.(64|65|66|67)\./, /^192\.168\./, ...envRegexes('FLEET_CUSTOMER_SUBNETS')];
const CUSTOMER_NAME = [/\/id:\d/i, /\bcpe\b/i, /customer/i, /th house/i, ...envRegexes('FLEET_CUSTOMER_NAMES')];

function arg(name, def = null) {
  const i = process.argv.indexOf(`--${name}`);
  if (i < 0) return def;
  if (['apply'].includes(name)) return true;
  return process.argv[i + 1] ?? def;
}
function fail(m) { console.error(`fleet_bind_sweep: ${m}`); process.exit(1); }

const passphrase = resolvePassphrase();
if (!passphrase) fail('NOC_MCP_PASSPHRASE is not set (environment or .env)');
const siteFrag = (arg('site') || '').toLowerCase();
if (!siteFrag) fail('--site <fragment> is required to bound the sweep');
const limit = Number(arg('limit', '50'));
const apply = Boolean(arg('apply'));

function banner(ip, ms = 6000) {
  return new Promise((resolve) => {
    const s = new Socket();
    let buf = '';
    const done = (v) => { try { s.destroy(); } catch {} resolve(v); };
    const t = setTimeout(() => done(null), ms);
    s.on('data', (d) => { buf += d.toString(); if (buf.length > 200 || buf.includes('\n')) { clearTimeout(t); done(buf.trim()); } });
    s.on('error', () => { clearTimeout(t); done(null); });
    s.connect(22, ip);
  });
}

const SAFE_USER = /^[A-Za-z0-9._-]{1,64}$/;

function sshEcho(ip, username, password, ms = 20000) {
  // Returns 'ok' | 'auth-rejected' | 'transport-fail'. Transport failures must
  // NEVER be read as wrong-password (field lesson): they abort probing
  // for the host instead of falling through to the next credential.
  // Password goes to `sshpass -e` through the child's environment (never argv);
  // host keys follow SSH_HOSTKEY_CHECKING (default accept-new).
  return new Promise((resolve) => {
    if (isIP(ip) !== 4 || !SAFE_USER.test(username || '')) return resolve('transport-fail');
    execFile('sshpass', ['-e', 'ssh', ...sshOptionArgs({ legacyAlgos: true, connectTimeout: 10 }),
      `${username}@${ip}`, 'echo BIND-OK'], { timeout: ms + 5000, env: { ...process.env, SSHPASS: password } }, (err, stdout, stderr) => {
      if (!err && stdout.trim() === 'BIND-OK') return resolve('ok');
      const e = ((stderr || '') + ' ' + (err?.message || '')).toLowerCase();
      if (/permission denied|authentication failed|wrong password/.test(e)) return resolve('auth-rejected');
      resolve('transport-fail');
    });
  });
}

const normId = (name) => (name.toLowerCase().match(/["']?([a-z0-9]+)/) || [null, 'unknown'])[1];
const norm = (s) => (s || '').trim().toLowerCase();

if (!existsSync(CACHE)) fail('no inventory cache; run scripts/inventory_resolve.mjs <name> --refresh first');
const devices = JSON.parse(readFileSync(CACHE, 'utf8')).devices
  .filter((d) => ((d.name || '').toLowerCase().includes(siteFrag) || (d.site || '').toLowerCase().includes(siteFrag)))
  .filter((d) => d.mgmt_ip && d.status === 'up')
  .slice(0, limit);

let store;
try { store = decryptStore(STORE, passphrase); } catch (e) { fail(e.message); }
const pool = (store.deviceTypes.find((g) => g.deviceType === 'ubnt-airos')?.credentials || [])
  .slice().sort((a, b) => b.priority - a.priority);
const existing = new Set((store.exactDeviceCredentials || []).map((b) => `${b.deviceType}|${norm(b.deviceId)}|${norm(b.host)}`));
const boundHosts = new Set((store.exactDeviceCredentials || []).filter((b) => b.deviceType === 'ubnt-airos').map((b) => norm(b.host)));

const report = { site: siteFrag, scanned: devices.length, bound: [], skippedCustomer: [], skippedHaveBinding: [], nonAiros: [], unreachable: [], authFailed: [] };
const staged = [];

for (const dev of devices) {
  const name = dev.name || '';
  if (CUSTOMER_SUBNETS.some((re) => re.test(dev.mgmt_ip || '')) || CUSTOMER_NAME.some((re) => re.test(name))) {
    report.skippedCustomer.push({ id: dev.id, name, mgmt_ip: dev.mgmt_ip });
    continue;
  }
  const id = normId(name);
  if (existing.has(`ubnt-airos|${id}|${norm(dev.mgmt_ip)}`) || boundHosts.has(norm(dev.mgmt_ip))) {
    report.skippedHaveBinding.push({ id: dev.id, name, mgmt_ip: dev.mgmt_ip });
    continue;
  }
  const ban = await banner(dev.mgmt_ip);
  if (!ban || !/ssh-2\.0-dropbear/i.test(ban)) {
    report.nonAiros.push({ id: dev.id, name, mgmt_ip: dev.mgmt_ip, banner: (ban || 'unreachable').slice(0, 40) });
    continue;
  }
  let bound = null;
  let unreachable = false;
  for (const cred of pool) {
    const verdict = await sshEcho(dev.mgmt_ip, cred.username, cred.password);
    if (verdict === 'ok') { bound = cred.name; break; }
    if (verdict === 'transport-fail') { unreachable = true; break; }
  }
  if (unreachable) {
    report.unreachable.push({ id: dev.id, name, mgmt_ip: dev.mgmt_ip });
    continue;
  }
  if (!bound) {
    report.authFailed.push({ id: dev.id, name, mgmt_ip: dev.mgmt_ip });
    continue;
  }
  staged.push({ deviceType: 'ubnt-airos', deviceId: id, host: dev.mgmt_ip, pool: bound, name });
  report.bound.push({ id: dev.id, deviceId: id, host: dev.mgmt_ip, pool: bound, name });
}

console.log(JSON.stringify(report, null, 2));
if (staged.length && apply) {
  for (const st of staged) {
    const poolCred = pool.find((c) => c.name === st.pool);
    store.exactDeviceCredentials.push({ deviceType: st.deviceType, deviceId: st.deviceId, host: st.host, credential: { ...poolCred } });
  }
  writeStoreAtomic(STORE, store, passphrase);   // backup + 0600 temp + fsync + rename; keeps the store's format
  console.log(JSON.stringify({ stored: staged.length }));
} else {
  console.log(JSON.stringify({ stored: 0, note: staged.length ? 'dry-run; re-run with --apply' : 'nothing to bind' }));
}
