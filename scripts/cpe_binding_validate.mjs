#!/usr/bin/env node
// Validates a per-incident CPE binding file WITHOUT printing the password.
// Staged file lives outside the repo, mode 0600, shaped as:
// { "deviceType": "ubnt-airos", "deviceId": "<uisp-id-or-mac>", "host": "192.0.2.25",
//   "credential": { "name": "cpe-<incident>", "username": "ubnt", "password": "<secret>" } }
// Operator holds the password locally for the single maintenance window and deletes
// the staged file afterwards. Nothing here provisions long-term shared storage.
import { readFileSync, statSync, realpathSync } from 'node:fs';
import { dirname, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isIP } from 'node:net';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function fail(msg) { console.error(`CPE binding error: ${msg}`); process.exitCode = 1; }

const file = process.argv[2];
if (!file || process.argv.includes('--help') || process.argv.includes('-h')) {
  console.log('Usage: node scripts/cpe_binding_validate.mjs /root/.secrets/cpe-<incident>.json');
  process.exit(process.argv[2] ? 1 : 0);
}
try {
  const resolved = realpathSync(resolve(file));
  const rel = relative(realpathSync(ROOT), resolved);
  if (rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`))) fail('binding file must be outside the repository');
  else {
    const stats = statSync(resolved);
    if (!stats.isFile()) fail('binding file must be a regular file');
    else if (process.platform !== 'win32' && (stats.mode & 0o077) !== 0) fail('binding file permissions too broad; use chmod 600');
    else {
      const b = JSON.parse(readFileSync(resolved, 'utf8'));
      const errors = [];
      if (b.deviceType !== 'ubnt-airos') errors.push('deviceType must be ubnt-airos');
      if (typeof b.deviceId !== 'string' || !b.deviceId.trim() || b.deviceId.length > 128) errors.push('deviceId must be a non-empty stable ID (UISP ID or MAC)');
      if (typeof b.host !== 'string' || isIP(b.host.trim()) !== 4) errors.push('host must be an exact IPv4 management address');
      const c = b.credential || {};
      if (typeof c.username !== 'string' || !c.username.trim()) errors.push('credential.username is required');
      if (typeof c.password !== 'string' || !c.password) errors.push('credential.password is required (passed only via this file, never CLI/chat)');
      if (typeof c.name !== 'string' || !c.name.trim()) errors.push('credential.name is required (use cpe-<incident>)');
      if (errors.length) fail(errors.join('; '));
      else {
        console.log(JSON.stringify({
          valid: true,
          deviceType: b.deviceType,
          deviceId: b.deviceId.trim(),
          host: b.host.trim(),
          credentialName: c.name,
          username: c.username,
          password: '<redacted>',
          note: 'Shape valid. Operator-held single-use only: use locally for the maintenance window, then delete the staged file. Do not add to shared type-wide credentials.',
        }, null, 2));
      }
    }
  }
} catch (e) { fail(e.message); }
