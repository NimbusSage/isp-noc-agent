#!/usr/bin/env node
// Provision (or remove) an exact-device credential binding in the encrypted
// MCP store by pinning an ALREADY-CONFIGURED pool credential to one
// device ID + host. No new secrets are introduced and none are printed.
//
//   List pool creds (names only):
//     node scripts/provision_exact_binding.mjs --list --device-type ubnt-airos
//   Dry-run:
//     node scripts/provision_exact_binding.mjs --device-type ubnt-airos \
//       --device-id airos-ap-1 --host 10.0.28.113 --from-pool default
//   Apply (backs up the store first):
//     ... --apply
//   Remove:
//     node scripts/provision_exact_binding.mjs --remove \
//       --device-type ubnt-airos --device-id airos-ap-1 --host 10.0.28.113 --apply
//
// Passphrase: NOC_MCP_PASSPHRASE from the environment, else the .env file (scripts/cred_store.mjs).
// The store is rewritten atomically (backup copy, 0600 temp file, fsync, rename) in the SAME
// format it was read in (NOCENC2 stays NOCENC2, legacy stays legacy). `--format v2|legacy`
// converts explicitly on --apply.
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { STORE_FORMATS, decryptStore, resolvePassphrase, writeStoreAtomic } from './cred_store.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const STORE = (() => {
  const i = process.argv.indexOf('--store');
  return (i >= 0 && process.argv[i + 1]) ? resolve(process.argv[i + 1]) : resolve(ROOT, 'mcp-server', 'config', 'credentials.enc');
})();

function arg(name) {
  const i = process.argv.indexOf(`--${name}`);
  if (i < 0) return null;
  if (['apply', 'list', 'remove'].includes(name)) return true;
  return process.argv[i + 1] ?? null;
}
function fail(msg) { console.error(`provision_exact_binding: ${msg}`); process.exit(1); }

const passphrase = resolvePassphrase();
if (!passphrase) fail('NOC_MCP_PASSPHRASE is not set (environment or .env)');

const formatFlag = arg('format');
if (formatFlag && !STORE_FORMATS.includes(formatFlag)) fail(`--format must be one of ${STORE_FORMATS.join(', ')}`);
const writeOpts = formatFlag ? { format: formatFlag } : {};
let store;
try { store = decryptStore(STORE, passphrase); } catch (e) { fail(e.message); }
store.exactDeviceCredentials ??= [];
const deviceType = arg('device-type') || 'ubnt-airos';

if (arg('list')) {
  const group = store.deviceTypes.find((g) => g.deviceType === deviceType);
  if (!group) fail(`unknown device type ${deviceType}`);
  console.log(JSON.stringify({
    deviceType,
    poolCredentials: group.credentials.map((c) => ({ name: c.name, username: c.username, priority: c.priority })),
    bindings: store.exactDeviceCredentials.filter((b) => b.deviceType === deviceType)
      .map((b) => ({ deviceId: b.deviceId, host: b.host, credential: b.credential.name })),
  }, null, 2));
  process.exit(0);
}

const deviceId = arg('device-id');
const host = arg('host');
if (!deviceId || !host) fail('--device-id and --host are required');
const norm = (s) => s.trim().toLowerCase();

if (arg('remove')) {
  const before = store.exactDeviceCredentials.length;
  store.exactDeviceCredentials = store.exactDeviceCredentials.filter((b) =>
    !(b.deviceType === deviceType && norm(b.deviceId) === norm(deviceId) && norm(b.host) === norm(host)));
  const removed = before - store.exactDeviceCredentials.length;
  console.log(JSON.stringify({ removed, deviceType, deviceId, host }));
  if (removed && arg('apply')) {
    writeStoreAtomic(STORE, store, passphrase, writeOpts);
    console.log(JSON.stringify({ stored: true }));
  } else if (removed) {
    console.log(JSON.stringify({ stored: false, note: 'dry-run; re-run with --apply' }));
  }
  process.exit(0);
}

const poolName = arg('from-pool');
if (!poolName) fail('--from-pool <pool-credential-name> is required');
const pool = store.deviceTypes.find((g) => g.deviceType === deviceType)?.credentials
  .find((c) => c.name === poolName);
if (!pool) fail(`pool credential '${poolName}' not found in ${deviceType}`);
const dup = store.exactDeviceCredentials.some((b) =>
  b.deviceType === deviceType && norm(b.deviceId) === norm(deviceId) && norm(b.host) === norm(host));
if (dup) fail('an identical binding already exists (duplicate bindings fail closed at runtime)');
const binding = {
  deviceType,
  deviceId: deviceId.trim(),
  host: host.trim(),
  credential: { ...pool },
};
console.log(JSON.stringify({
  wouldAdd: { deviceType, deviceId: binding.deviceId, host: binding.host, credential: poolName, username: pool.username },
  password: '<pinned-from-pool, never printed>',
}));
if (arg('apply')) {
  store.exactDeviceCredentials.push(binding);
  writeStoreAtomic(STORE, store, passphrase, writeOpts);
  console.log(JSON.stringify({ stored: true, backup: 'credentials.enc.bak-<ts> (remove after verifying)' }));
} else {
  console.log(JSON.stringify({ stored: false, note: 'dry-run; re-run with --apply' }));
}
