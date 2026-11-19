#!/usr/bin/env node
// bind_device.mjs - pin an already-configured pool credential to ONE device
// (device ID + host), after proving it authenticates there.
//
// Why this exists: exact-device bindings are what the gated write executors
// require (routeros-write, routeros-temp-access, temp-*). Keeping them by hand
// does not scale and drifts; the agent provisions them on demand instead.
// No new secret is introduced and none is printed: the winner is COPIED from a
// pool credential that already exists in the encrypted store.
//
//   List what would be probed (names only, no connections):
//     node scripts/bind_device.mjs --device-type mikrotik-ssh --device-id <device-id> \
//       --host <router-ip> --dry-run
//   Probe + pin the first credential that authenticates:
//     node scripts/bind_device.mjs --device-type mikrotik-ssh --device-id <device-id> \
//       --host <router-ip> --apply
//   Pin a specific pool credential (skips probing):
//     ... --pool admin-primary --apply
//   Inspect existing bindings for a device type:
//     node scripts/bind_device.mjs --device-type mikrotik-ssh --list
//   Remove one:
//     node scripts/bind_device.mjs --device-type mikrotik-ssh --device-id <device-id> \
//       --host <router-ip> --remove --apply
//
// Safety:
//   * Host must be a private/link-local/loopback address (never a public host).
//   * Probing = one harmless `echo BIND-OK` per credential, in pool priority
//     order, password through the child's environment (sshpass -e, never -p).
//   * Transport failure aborts the candidate instead of spraying other
//     passwords (a timeout is not a wrong password; field lesson).
//   * A duplicate binding fails closed at runtime, so it is refused here.
//   * The store is rewritten atomically, in the format it was read (backup copy,
//     0600 temp, fsync, rename) by scripts/cred_store.mjs.
import { execFile } from 'node:child_process';
import { isIP } from 'node:net';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { decryptStore, resolvePassphrase, writeStoreAtomic, defaultStorePath } from './cred_store.mjs';
import { sshOptionArgs } from './ssh_opts.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const STORE = defaultStorePath(ROOT);

function arg(name) {
  const i = process.argv.indexOf(`--${name}`);
  if (i < 0) return null;
  if (['apply', 'list', 'remove', 'dry-run'].includes(name)) return true;
  return process.argv[i + 1] ?? null;
}
function fail(msg) {
  console.error(`bind_device: ${msg}`);
  process.exit(1);
}

const DEVICE_TYPES = ['mikrotik-ssh', 'mikrotik-rest', 'ubnt-uisp', 'ubnt-airos', 'ubnt-wave', 'customer-airos', 'customer-mt'];
const norm = (s) => (s || '').trim().toLowerCase();

/** Private, link-local or loopback only: credentials never leave those ranges here. */
function isPrivateHost(host) {
  if (isIP(host) === 4) {
    const [a, b] = host.split('.').map(Number);
    return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254);
  }
  return host === 'localhost';
}

const passphrase = resolvePassphrase();
if (!passphrase) fail('NOC_MCP_PASSPHRASE is not set (environment or .env)');

let store;
try {
  store = decryptStore(STORE, passphrase);
} catch (e) {
  fail(e.message);
}
store.exactDeviceCredentials ??= [];

const deviceType = arg('device-type') || fail('--device-type is required');
if (!DEVICE_TYPES.includes(deviceType)) fail(`--device-type must be one of ${DEVICE_TYPES.join(', ')}`);
const group = store.deviceTypes.find((g) => g.deviceType === deviceType);
if (!group) fail(`no credentials configured for ${deviceType}`);

if (arg('list')) {
  console.log(JSON.stringify({
    deviceType,
    pool: group.credentials
      .slice()
      .sort((a, b) => b.priority - a.priority)
      .map((c) => ({ name: c.name, username: c.username, priority: c.priority })),
    bindings: store.exactDeviceCredentials
      .filter((b) => b.deviceType === deviceType)
      .map((b) => ({ deviceId: b.deviceId, host: b.host, credential: b.credential.name })),
  }, null, 2));
  process.exit(0);
}

const deviceId = arg('device-id');
const host = arg('host');
if (!deviceId || !host) fail('--device-id and --host are required');
if (!isPrivateHost(host) && deviceType !== 'ubnt-uisp') fail(`${host} is not a private/link-local/loopback address; refusing to send credentials there`);

const matches = store.exactDeviceCredentials.filter((b) =>
  b.deviceType === deviceType && norm(b.deviceId) === norm(deviceId) && norm(b.host) === norm(host));

if (arg('remove')) {
  if (!matches.length) fail('no identical binding exists; nothing to remove');
  store.exactDeviceCredentials = store.exactDeviceCredentials.filter((b) => !matches.includes(b));
  console.log(JSON.stringify({ removed: matches.length, deviceType, deviceId, host }));
  if (arg('apply')) {
    writeStoreAtomic(STORE, store, passphrase);
    console.log(JSON.stringify({ stored: true }));
  } else {
    console.log(JSON.stringify({ stored: false, note: 'dry-run; re-run with --apply' }));
  }
  process.exit(0);
}

if (matches.length) {
  fail(`an identical binding already exists (credential '${matches[0].credential.name}'); duplicate bindings fail closed at runtime`);
}

const pool = group.credentials
  .filter((c) => !arg('pool') || c.name === arg('pool'))
  .slice()
  .sort((a, b) => b.priority - a.priority);
if (!pool.length) fail(`pool credential '${arg('pool')}' not found in ${deviceType}`);

/** One harmless auth probe. 'ok' | 'auth-rejected' | 'transport-fail'. */
function probe(host, username, password, timeoutMs = 20000) {
  return new Promise((done) => {
    execFile('sshpass', ['-e', 'ssh', ...sshOptionArgs({ legacyAlgos: deviceType !== 'ubnt-wave', connectTimeout: 10 }),
      `${username}@${host}`, 'echo BIND-OK'],
    { timeout: timeoutMs + 5000, env: { ...process.env, SSHPASS: password } }, (err, stdout, stderr) => {
      if (!err && stdout.trim() === 'BIND-OK') return done('ok');
      const text = `${stderr || ''} ${err?.message || ''}`.toLowerCase();
      if (/permission denied|authentication failed|wrong password/.test(text)) return done('auth-rejected');
      done('transport-fail');
    });
  });
}

const pinned = arg('pool') ? pool[0] : null;
const attempts = [];
let winner = pinned;
if (!winner) {
  for (const cred of pool) {
    const verdict = await probe(host, cred.username, cred.password);
    attempts.push({ credential: cred.name, verdict });
    if (verdict === 'ok') { winner = cred; break; }
    if (verdict === 'transport-fail') break; // host unreachable: do not spray the rest
  }
}

if (!winner) {
  console.log(JSON.stringify({ deviceType, deviceId, host, attempts, bound: null, stored: false,
    note: 'no pool credential authenticated; check the host, the pool, or add the credential first' }, null, 2));
  process.exit(2);
}

const binding = { deviceType, deviceId: deviceId.trim(), host: host.trim(), credential: { ...winner } };
console.log(JSON.stringify({
  wouldAdd: { deviceType, deviceId: binding.deviceId, host: binding.host, credential: winner.name, username: winner.username },
  attempts,
  password: '<pinned-from-pool, never printed>',
}, null, 2));

if (arg('apply') && !arg('dry-run')) {
  store.exactDeviceCredentials.push(binding);
  writeStoreAtomic(STORE, store, passphrase);
  console.log(JSON.stringify({ stored: true, note: 'store rewritten atomically (backup copy kept beside it)' }));
} else {
  console.log(JSON.stringify({ stored: false, note: 'dry-run; re-run with --apply' }));
}
