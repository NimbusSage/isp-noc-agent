import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { checkHostKey, hostkeyMode, knownHostsFile, makeHostVerifier, sshOptionArgs } from './ssh_opts.mjs';

function envIn(extra = {}) {
  const d = mkdtempSync(path.join(os.tmpdir(), 'sshopts-'));
  return { d, env: { XDG_CACHE_HOME: d, ...extra }, cleanup: () => rmSync(d, { recursive: true, force: true }) };
}

test('default mode is accept-new with a dedicated known_hosts file (dir 0700)', () => {
  const t = envIn();
  try {
    assert.equal(hostkeyMode(t.env), 'accept-new');
    const args = sshOptionArgs({ env: t.env });
    assert.ok(args.includes('StrictHostKeyChecking=accept-new'));
    const kh = args.find((a) => a.startsWith('UserKnownHostsFile='));
    assert.equal(kh, `UserKnownHostsFile=${path.join(t.d, 'nocagent', 'known_hosts')}`);
    assert.equal(statSync(path.join(t.d, 'nocagent')).mode & 0o777, 0o700);
    assert.ok(!args.join(' ').includes('StrictHostKeyChecking=no'));
  } finally { t.cleanup(); }
});

test('SSH_HOSTKEY_CHECKING: yes / no / invalid', () => {
  const t = envIn();
  try {
    assert.ok(sshOptionArgs({ env: { ...t.env, SSH_HOSTKEY_CHECKING: 'yes' } }).includes('StrictHostKeyChecking=yes'));
    const noArgs = sshOptionArgs({ env: { ...t.env, SSH_HOSTKEY_CHECKING: 'no' } });
    assert.ok(noArgs.includes('StrictHostKeyChecking=no') && noArgs.includes('UserKnownHostsFile=/dev/null'));
    assert.throws(() => sshOptionArgs({ env: { ...t.env, SSH_HOSTKEY_CHECKING: 'off' } }), /must be one of/);
    assert.equal(knownHostsFile({ SSH_KNOWN_HOSTS_FILE: path.join(t.d, 'kh') }), path.join(t.d, 'kh'));
  } finally { t.cleanup(); }
});

test('legacy algorithms are opt-in', () => {
  const t = envIn();
  try {
    assert.ok(!sshOptionArgs({ env: t.env }).some((a) => a.includes('ssh-rsa')));
    assert.ok(sshOptionArgs({ env: t.env, legacyAlgos: true }).some((a) => a.includes('ssh-rsa')));
  } finally { t.cleanup(); }
});

test('checkHostKey: first contact, match, change, strict mode, disabled', () => {
  const fp = 'a'.repeat(64);
  assert.deepEqual(checkHostKey({ mode: 'accept-new', known: {}, host: 'h', fingerprint: fp }), { ok: true, remember: true });
  assert.deepEqual(checkHostKey({ mode: 'accept-new', known: { h: fp }, host: 'h', fingerprint: fp }), { ok: true, remember: false });
  const changed = checkHostKey({ mode: 'accept-new', known: { h: 'b'.repeat(64) }, host: 'h', fingerprint: fp });
  assert.equal(changed.ok, false);
  assert.match(changed.reason, /CHANGED/);
  assert.equal(checkHostKey({ mode: 'yes', known: {}, host: 'h', fingerprint: fp }).ok, false);
  assert.equal(checkHostKey({ mode: 'no', known: { h: 'b'.repeat(64) }, host: 'h', fingerprint: fp }).ok, true);
});

test('makeHostVerifier remembers on first contact and refuses a changed key', () => {
  const t = envIn();
  try {
    const logs = [];
    const verify = (host) => makeHostVerifier(host, { env: t.env, log: (m) => logs.push(m) });
    assert.equal(verify('10.0.0.1')('f1'.repeat(32)), true);
    const file = path.join(t.d, 'nocagent', 'ssh2_hostkeys.json');
    assert.equal(statSync(file).mode & 0o777, 0o600);
    assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), { '10.0.0.1': 'f1'.repeat(32) });
    assert.equal(verify('10.0.0.1')('f1'.repeat(32)), true);
    assert.equal(verify('10.0.0.1')('f2'.repeat(32)), false);
    assert.ok(logs.some((l) => /REFUSED.*CHANGED/.test(l)));
    assert.equal(verify('10.0.0.2')('f3'.repeat(32)), true);           // other hosts unaffected
    const strict = makeHostVerifier('10.0.0.9', { env: { ...t.env, SSH_HOSTKEY_CHECKING: 'yes' }, log: () => {} });
    assert.equal(strict('f4'.repeat(32)), false);
    const off = makeHostVerifier('10.0.0.1', { env: { ...t.env, SSH_HOSTKEY_CHECKING: 'no' }, log: () => {} });
    assert.equal(off('anything'), true);
  } finally { t.cleanup(); }
});
