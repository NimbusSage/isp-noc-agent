import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  decryptStore, defaultStorePath, encryptStore, loadStore, parseDotenvValue, poolCredentials, resolvePassphrase, writeStoreAtomic,
} from './cred_store.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PASS = 'test-passphrase-' + 'x'.repeat(8);
const STORE = {
  version: 1,
  deviceTypes: [
    { deviceType: 'mikrotik-ssh', credentials: [
      { name: 'low', username: 'admin', password: 'pw-low', priority: 1, tags: [] }, // secret-scan:allow(fake credential or address fixture for an offline test)
      { name: 'high', username: 'admin', password: 'pw-high', priority: 9, tags: [] }, // secret-scan:allow(fake credential or address fixture for an offline test)
      { name: 'other', username: 'ops', password: 'pw-ops', priority: 5, tags: [] }, // secret-scan:allow(fake credential or address fixture for an offline test)
    ], policy: {} },
    { deviceType: 'ubnt-uisp', credentials: [{ name: 'uisp', username: 'u', password: '', apiToken: 'tok-123', priority: 1, tags: [] }], policy: {} },
  ],
  exactDeviceCredentials: [],
};

function tmp() {
  const d = mkdtempSync(path.join(os.tmpdir(), 'credstore-'));
  return { d, cleanup: () => rmSync(d, { recursive: true, force: true }) };
}

test('encrypt/decrypt round trip; wrong passphrase and tampering fail with a clear message', () => {
  const t = tmp();
  try {
    const f = path.join(t.d, 'credentials.enc');
    writeFileSync(f, encryptStore(STORE, PASS));
    assert.deepEqual(decryptStore(f, PASS), STORE);
    assert.throws(() => decryptStore(f, 'nope'), /decryption failed/);
    const buf = readFileSync(f); buf[buf.length - 1] ^= 1; writeFileSync(f, buf);
    assert.throws(() => decryptStore(f, PASS), /decryption failed/);
  } finally { t.cleanup(); }
});

test('poolCredentials sorts by priority (highest first)', () => {
  assert.deepEqual(poolCredentials(STORE, 'mikrotik-ssh').map((c) => c.name), ['high', 'other', 'low']);
  assert.deepEqual(poolCredentials(STORE, 'nope'), []);
});

test('loadStore needs a passphrase and the file', () => {
  const t = tmp();
  try {
    assert.throws(() => loadStore({ file: path.join(t.d, 'x.enc'), passphrase: '' }), /NOC_MCP_PASSPHRASE is not set/);
    assert.throws(() => loadStore({ file: path.join(t.d, 'missing.enc'), passphrase: PASS }), /not found/);
  } finally { t.cleanup(); }
});

test('writeStoreAtomic: backup, 0600, no temp leftovers, round trip', () => {
  const t = tmp();
  try {
    const f = path.join(t.d, 'credentials.enc');
    writeStoreAtomic(f, STORE, PASS);
    assert.equal(statSync(f).mode & 0o777, 0o600);
    writeStoreAtomic(f, { ...STORE, exactDeviceCredentials: [] }, PASS);
    const names = readdirSync(t.d);
    assert.equal(names.filter((n) => n.includes('.bak-')).length, 1);
    assert.equal(names.filter((n) => n.endsWith('.tmp')).length, 0);
    assert.equal(statSync(path.join(t.d, names.find((n) => n.includes('.bak-')))).mode & 0o777, 0o600);
    assert.deepEqual(decryptStore(f, PASS), STORE);
  } finally { t.cleanup(); }
});

test('parseDotenvValue reads plain data only (quotes, export, comments) - no shell evaluation', () => {
  const env = 'A=1\nexport NOC_MCP_PASSPHRASE="phrase with # hash" # c\nB=$(touch /tmp/should-not-exist)\n';
  assert.equal(parseDotenvValue(env, 'NOC_MCP_PASSPHRASE'), 'phrase with # hash');
  assert.equal(parseDotenvValue('NOC_MCP_PASSPHRASE=abc # note\n', 'NOC_MCP_PASSPHRASE'), 'abc');
  assert.equal(parseDotenvValue("NOC_MCP_PASSPHRASE='single quoted $HOME'\n", 'NOC_MCP_PASSPHRASE'), 'single quoted $HOME');
  assert.equal(parseDotenvValue('OTHER=1\n', 'NOC_MCP_PASSPHRASE'), '');
});

test('resolvePassphrase: environment first, then the .env file, else empty', () => {
  const t = tmp();
  try {
    const dotenv = path.join(t.d, '.env');
    writeFileSync(dotenv, 'NOC_MCP_PASSPHRASE=from-file\n'); // secret-scan:allow(fake credential or address fixture for an offline test)
    assert.equal(resolvePassphrase({ NOC_MCP_PASSPHRASE: ' from-env ', NOC_DOTENV: dotenv }), 'from-env');
    assert.equal(resolvePassphrase({ NOC_DOTENV: dotenv }), 'from-file');
    assert.equal(resolvePassphrase({ NOC_DOTENV: path.join(t.d, 'absent') }), '');
  } finally { t.cleanup(); }
});

test('defaultStorePath honours NOC_CREDENTIALS_ENC', () => {
  assert.equal(defaultStorePath('/r', { NOC_CREDENTIALS_ENC: '/x/y.enc' }), '/x/y.enc');
  assert.equal(defaultStorePath('/r', {}), path.resolve('/r', 'mcp-server', 'config', 'credentials.enc'));
});

function runCli(args, env) {
  return spawnSync(process.execPath, [path.join(HERE, 'cred_store.mjs'), ...args], {
    encoding: 'utf8', env: { PATH: process.env.PATH, ...env },
  });
}

test('CLI prints the pool as JSON over a pipe, filtered by user, never without --reveal', () => {
  const t = tmp();
  try {
    const f = path.join(t.d, 'credentials.enc');
    writeFileSync(f, encryptStore(STORE, PASS));
    const env = { NOC_CREDENTIALS_ENC: f, NOC_MCP_PASSPHRASE: PASS, NOC_DOTENV: path.join(t.d, 'absent') };
    const ok = runCli(['pool', 'mikrotik-ssh', '--user', 'admin', '--reveal'], env);
    assert.equal(ok.status, 0, ok.stderr);
    assert.deepEqual(JSON.parse(ok.stdout).map((c) => c.password), ['pw-high', 'pw-low']);
    const tok = runCli(['pool', 'ubnt-uisp', '--reveal'], env);
    assert.equal(JSON.parse(tok.stdout)[0].apiToken, 'tok-123');
    const noReveal = runCli(['pool', 'mikrotik-ssh'], env);
    assert.equal(noReveal.status, 1);
    assert.ok(!noReveal.stdout.includes('pw-'));
    const none = runCli(['pool', 'ubnt-wave', '--reveal'], env);
    assert.equal(none.status, 4);
    const noPass = runCli(['pool', 'mikrotik-ssh', '--reveal'], { ...env, NOC_MCP_PASSPHRASE: '' });
    assert.equal(noPass.status, 2);
    const wrong = runCli(['pool', 'mikrotik-ssh', '--reveal'], { ...env, NOC_MCP_PASSPHRASE: 'wrong' }); // secret-scan:allow(fake credential or address fixture for an offline test)
    assert.equal(wrong.status, 3);
    assert.ok(!wrong.stderr.includes(PASS));
  } finally { t.cleanup(); }
});
