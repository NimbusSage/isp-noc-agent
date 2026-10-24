import test from 'node:test';
import assert from 'node:assert/strict';
import { chmodSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { cliInit, parseCliArgs, resolveClientSecret, storeRefreshToken, writeSecretFile } from './google_user_auth.mjs';

const mode = (f) => statSync(f).mode & 0o777;
function tmp() {
  const d = mkdtempSync(path.join(os.tmpdir(), 'gauth-'));
  return { d, cleanup: () => rmSync(d, { recursive: true, force: true }) };
}

test('writeSecretFile creates the file 0600 (and its missing parent 0700) with no temp leftovers', () => {
  const t = tmp();
  try {
    const f = path.join(t.d, 'conf', 'oauth.json');
    writeSecretFile(f, '{"a":1}');
    assert.equal(mode(f), 0o600);
    assert.equal(mode(path.dirname(f)), 0o700);
    assert.equal(readFileSync(f, 'utf8'), '{"a":1}');
    assert.deepEqual(readdirSync(path.dirname(f)), ['oauth.json']);
  } finally { t.cleanup(); }
});

test('writeSecretFile refuses a pre-existing file that is wider than owner-only', () => {
  const t = tmp();
  try {
    const f = path.join(t.d, 'oauth.json');
    writeFileSync(f, 'old');
    chmodSync(f, 0o644);
    assert.throws(() => writeSecretFile(f, 'new'), /wider than owner-only/);
    assert.equal(readFileSync(f, 'utf8'), 'old');
    chmodSync(f, 0o600);
    writeSecretFile(f, 'new');                       // owner-only existing file may be replaced
    assert.equal(readFileSync(f, 'utf8'), 'new');
    assert.equal(mode(f), 0o600);
  } finally { t.cleanup(); }
});

test('writeSecretFile refuses a symlink target and any path inside the repository', () => {
  const t = tmp();
  try {
    const real = path.join(t.d, 'real.json');
    writeFileSync(real, 'keep', { mode: 0o600 });
    const link = path.join(t.d, 'link.json');
    symlinkSync(real, link);
    assert.throws(() => writeSecretFile(link, 'x'), /not a regular file/);
    assert.equal(readFileSync(real, 'utf8'), 'keep');
    const inRepo = new URL('../.gauth-test-should-not-exist.json', import.meta.url).pathname;
    assert.throws(() => writeSecretFile(inRepo, 'x'), /outside the repository/);
    assert.equal(existsSync(inRepo), false);
  } finally { t.cleanup(); }
});

test('--client-secret on argv is rejected with a deprecation message', () => {
  const args = parseCliArgs(['node', 'x', 'init', '--oauth-file', '/x', '--client-id', 'id', '--client-secret', 'hunter2']);
  assert.throws(() => resolveClientSecret(args, {}), /no longer accepted.*--client-secret-file.*GOOGLE_CLIENT_SECRET/s);
  assert.throws(() => cliInit({ ...args, 'oauth-file': '/tmp/never-written.json' }, {}), /no longer accepted/);
  assert.equal(existsSync('/tmp/never-written.json'), false);
});

test('client secret from an owner-only file or GOOGLE_CLIENT_SECRET', () => {
  const t = tmp();
  try {
    const sf = path.join(t.d, 'secret.txt');
    writeFileSync(sf, 'file-secret-value\n', { mode: 0o600 });
    assert.equal(resolveClientSecret({ 'client-secret-file': sf }, {}), 'file-secret-value');
    assert.equal(resolveClientSecret({}, { GOOGLE_CLIENT_SECRET: ' env-secret ' }), 'env-secret');
    chmodSync(sf, 0o644);
    assert.throws(() => resolveClientSecret({ 'client-secret-file': sf }, {}), /permissions are too broad/);
    assert.throws(() => resolveClientSecret({}, {}), /client secret missing/);
    assert.throws(() => resolveClientSecret({}, { GOOGLE_CLIENT_SECRET: 'a\nb' }), /client secret missing/); // secret-scan:allow(fake credential or address fixture for an offline test)
  } finally { t.cleanup(); }
});

test('init writes client_id/client_secret 0600 and storeRefreshToken keeps the mode', () => {
  const t = tmp();
  try {
    const f = path.join(t.d, 'oauth.json');
    cliInit({ 'oauth-file': f, 'client-id': 'cid-123' }, { GOOGLE_CLIENT_SECRET: 'csecret' });
    assert.equal(mode(f), 0o600);
    assert.deepEqual(JSON.parse(readFileSync(f, 'utf8')), { client_id: 'cid-123', client_secret: 'csecret' });
    storeRefreshToken(f, 'refresh-abc');
    assert.equal(mode(f), 0o600);
    assert.deepEqual(JSON.parse(readFileSync(f, 'utf8')), { client_id: 'cid-123', client_secret: 'csecret', refresh_token: 'refresh-abc' });
    assert.deepEqual(readdirSync(t.d), ['oauth.json']);
  } finally { t.cleanup(); }
});

test('init needs --oauth-file and --client-id', () => {
  assert.throws(() => cliInit({ 'client-id': 'x' }, { GOOGLE_CLIENT_SECRET: 's' }), /usage: init/);
  assert.throws(() => cliInit({ 'oauth-file': '/tmp/x.json' }, { GOOGLE_CLIENT_SECRET: 's' }), /usage: init/);
});
