// Store-format tests for scripts/cred_store.mjs. Fixtures are built at runtime, with an
// independent from-the-spec implementation written directly against node:crypto (layouts as
// documented in mcp-server/src/credentials/store.ts), so a mistake in cred_store.mjs cannot
// hide behind itself. Passphrases/passwords are made up per run.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createCipheriv, createDecipheriv, createHash, randomBytes, scryptSync } from 'node:crypto';
import { copyFileSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  STORE_FORMATS, decryptStoreBuffer, decryptStore, detectStoreFormat, encryptStore, poolCredentials, readStore, writeStoreAtomic,
} from './cred_store.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PASS = `pp-${randomBytes(6).toString('hex')}`;
const SECRET = `sw-${randomBytes(6).toString('hex')}`;
const STORE = {
  version: 1,
  deviceTypes: [{ deviceType: 'mikrotik-ssh', policy: {}, credentials: [{ name: 'a', username: 'admin', password: SECRET, priority: 3, tags: [] }] }],
  exactDeviceCredentials: [],
};
const MAGIC = Buffer.from('NOCENC2', 'ascii');

// ---- independent reference implementations (spec layouts) ----
function specLegacy(data, pass) {
  const iv = randomBytes(16);
  const c = createCipheriv('aes-256-gcm', createHash('sha256').update(pass).digest(), iv);
  const ct = Buffer.concat([c.update(JSON.stringify(data), 'utf8'), c.final()]);
  return Buffer.concat([iv, c.getAuthTag(), ct]);
}
function specV2(data, pass) {
  const salt = randomBytes(16);
  const iv = randomBytes(12);
  const key = scryptSync(pass, salt, 32, { N: 32768, r: 8, p: 1, maxmem: 64 * 1024 * 1024 });
  const c = createCipheriv('aes-256-gcm', key, iv);
  c.setAAD(Buffer.concat([MAGIC, salt]));
  const ct = Buffer.concat([c.update(JSON.stringify(data), 'utf8'), c.final()]);
  return Buffer.concat([MAGIC, salt, iv, c.getAuthTag(), ct]);
}
function specDecryptV2(buf, pass) {
  const salt = buf.subarray(7, 23);
  const iv = buf.subarray(23, 35);
  const tag = buf.subarray(35, 51);
  const key = scryptSync(pass, salt, 32, { N: 32768, r: 8, p: 1, maxmem: 64 * 1024 * 1024 });
  const d = createDecipheriv('aes-256-gcm', key, iv);
  d.setAAD(buf.subarray(0, 23));
  d.setAuthTag(tag);
  return JSON.parse(Buffer.concat([d.update(buf.subarray(51)), d.final()]).toString('utf8'));
}
function specDecryptLegacy(buf, pass) {
  const d = createDecipheriv('aes-256-gcm', createHash('sha256').update(pass).digest(), buf.subarray(0, 16));
  d.setAuthTag(buf.subarray(16, 32));
  return JSON.parse(Buffer.concat([d.update(buf.subarray(32)), d.final()]).toString('utf8'));
}

function tmp() {
  const d = mkdtempSync(path.join(os.tmpdir(), 'credfmt-'));
  return { d, cleanup: () => rmSync(d, { recursive: true, force: true }) };
}
const mode = (f) => statSync(f).mode & 0o777;

test('reads both formats written by an independent implementation', () => {
  const t = tmp();
  try {
    const l = path.join(t.d, 'legacy.enc'); writeFileSync(l, specLegacy(STORE, PASS));
    const v = path.join(t.d, 'v2.enc'); writeFileSync(v, specV2(STORE, PASS));
    assert.deepEqual(readStore(l, PASS), { data: STORE, format: 'legacy' });
    assert.deepEqual(readStore(v, PASS), { data: STORE, format: 'v2' });
    assert.deepEqual(decryptStore(v, PASS), STORE);
    assert.equal(poolCredentials(readStore(v, PASS).data, 'mikrotik-ssh')[0].password, SECRET);
  } finally { t.cleanup(); }
});

test('what cred_store writes is readable by the independent implementation (both directions)', () => {
  const v2 = encryptStore(STORE, PASS);                 // default format is v2
  assert.equal(detectStoreFormat(v2), 'v2');
  assert.deepEqual(v2.subarray(0, 7), MAGIC);
  assert.deepEqual(specDecryptV2(v2, PASS), STORE);
  const legacy = encryptStore(STORE, PASS, 'legacy');
  assert.equal(detectStoreFormat(legacy), 'legacy');
  assert.deepEqual(specDecryptLegacy(legacy, PASS), STORE);
});

test('fresh salt/iv on every encryption (no nonce reuse)', () => {
  const a = encryptStore(STORE, PASS);
  const b = encryptStore(STORE, PASS);
  assert.notDeepEqual(a.subarray(7, 35), b.subarray(7, 35));
  assert.notDeepEqual(encryptStore(STORE, PASS, 'legacy').subarray(0, 16), encryptStore(STORE, PASS, 'legacy').subarray(0, 16));
});

test('wrong passphrase fails closed with one generic error and leaks nothing', () => {
  for (const image of [specLegacy(STORE, PASS), specV2(STORE, PASS)]) {
    assert.throws(() => decryptStoreBuffer(image, 'not-it'), (e) => {
      assert.match(e.message, /decryption failed/);
      assert.ok(!e.message.includes(PASS) && !e.message.includes('not-it') && !e.message.includes(SECRET));
      return true;
    });
  }
});

test('tampering with ANY byte region of a v2 file fails closed (magic, salt, iv, tag, ciphertext, AAD)', () => {
  const good = specV2(STORE, PASS);
  const regions = { magic: 3, salt: 10, iv: 25, tag: 40, ciphertext: 60, last: good.length - 1 };
  for (const [name, idx] of Object.entries(regions)) {
    const bad = Buffer.from(good); bad[idx] ^= 0x01;
    assert.throws(() => decryptStoreBuffer(bad, PASS), /decryption failed/, `tamper ${name}`);
  }
});

test('tampering with any region of a legacy file fails closed', () => {
  const good = specLegacy(STORE, PASS);
  for (const idx of [0, 15, 16, 31, 32, good.length - 1]) {
    const bad = Buffer.from(good); bad[idx] ^= 0x80;
    assert.throws(() => decryptStoreBuffer(bad, PASS), /decryption failed/, `byte ${idx}`);
  }
});

test('truncated, empty and downgraded inputs fail closed', () => {
  const v2 = specV2(STORE, PASS);
  for (const n of [0, 1, 6, 7, 22, 23, 34, 50, 51]) {
    assert.throws(() => decryptStoreBuffer(v2.subarray(0, n), PASS), /decryption failed/, `v2 truncated to ${n}`);
  }
  const legacy = specLegacy(STORE, PASS);
  for (const n of [0, 5, 16, 31, 32]) {
    assert.throws(() => decryptStoreBuffer(legacy.subarray(0, n), PASS), /decryption failed/, `legacy truncated to ${n}`);
  }
  // downgrade: strip the v2 header and present the rest as a legacy file -> must not decrypt
  assert.throws(() => decryptStoreBuffer(v2.subarray(7), PASS), /decryption failed/);
  // upgrade: put a v2 magic in front of a legacy image -> must not decrypt
  assert.throws(() => decryptStoreBuffer(Buffer.concat([MAGIC, legacy]), PASS), /decryption failed/);
});

test('format detection is by magic only', () => {
  assert.equal(detectStoreFormat(Buffer.alloc(0)), 'legacy');
  assert.equal(detectStoreFormat(MAGIC), 'v2');
  assert.equal(detectStoreFormat(randomBytes(64).fill(0, 0, 1)), 'legacy');
});

test('re-writing keeps the existing format; new files default to v2; explicit format converts', () => {
  const t = tmp();
  try {
    const v = path.join(t.d, 'v2.enc'); writeFileSync(v, specV2(STORE, PASS), { mode: 0o600 });
    const l = path.join(t.d, 'legacy.enc'); writeFileSync(l, specLegacy(STORE, PASS), { mode: 0o600 });
    const n = path.join(t.d, 'new.enc');
    const changed = { ...STORE, exactDeviceCredentials: [{ deviceType: 'ubnt-airos', deviceId: 'x', host: '10.0.0.9', credential: STORE.deviceTypes[0].credentials[0] }] };

    assert.equal(writeStoreAtomic(v, changed, PASS), 'v2');
    assert.equal(detectStoreFormat(readFileSync(v)), 'v2');
    assert.deepEqual(specDecryptV2(readFileSync(v), PASS), changed);

    assert.equal(writeStoreAtomic(l, changed, PASS), 'legacy');
    assert.equal(detectStoreFormat(readFileSync(l)), 'legacy');
    assert.deepEqual(specDecryptLegacy(readFileSync(l), PASS), changed);

    assert.equal(writeStoreAtomic(n, changed, PASS), 'v2');
    assert.equal(detectStoreFormat(readFileSync(n)), 'v2');

    assert.equal(writeStoreAtomic(l, changed, PASS, { format: 'v2' }), 'v2');       // explicit upgrade
    assert.equal(detectStoreFormat(readFileSync(l)), 'v2');
    assert.equal(writeStoreAtomic(v, changed, PASS, { format: 'legacy' }), 'legacy'); // explicit downgrade
    assert.equal(detectStoreFormat(readFileSync(v)), 'legacy');
  } finally { t.cleanup(); }
});

test('atomic write: 0600, byte-identical 0600 backup, no temp leftovers, no secrets on stdout/stderr', () => {
  const t = tmp();
  try {
    const f = path.join(t.d, 'credentials.enc');
    const original = specV2(STORE, PASS);
    writeFileSync(f, original, { mode: 0o644 });          // a wide-open original must not stay wide open
    const out = [];
    const realLog = console.log; const realErr = console.error;
    console.log = (...a) => out.push(a.join(' ')); console.error = (...a) => out.push(a.join(' '));
    try { writeStoreAtomic(f, STORE, PASS); } finally { console.log = realLog; console.error = realErr; }
    assert.equal(mode(f), 0o600);
    const names = readdirSync(t.d);
    assert.equal(names.filter((x) => x.endsWith('.tmp')).length, 0);
    const bak = names.find((x) => x.includes('.bak-'));
    assert.ok(bak);
    assert.deepEqual(readFileSync(path.join(t.d, bak)), original);
    assert.equal(mode(path.join(t.d, bak)), 0o600);
    assert.equal(out.join('\n').includes(SECRET) || out.join('\n').includes(PASS), false);
  } finally { t.cleanup(); }
});

test('an invalid explicit format is rejected before the store or its backup is touched', () => {
  const t = tmp();
  try {
    const f = path.join(t.d, 'credentials.enc');
    const original = specV2(STORE, PASS);
    writeFileSync(f, original, { mode: 0o600 });
    assert.throws(() => writeStoreAtomic(f, STORE, PASS, { format: 'v3' }), /unknown store format/);
    assert.deepEqual(readFileSync(f), original);
    assert.equal(readdirSync(t.d).length, 1 + readdirSync(t.d).filter((x) => x.includes('.bak-')).length);
    assert.throws(() => encryptStore(STORE, PASS, 'nope'), /unknown store format/);
    assert.deepEqual(STORE_FORMATS, ['v2', 'legacy']);
  } finally { t.cleanup(); }
});

test('the pool CLI reads a v2 store and a legacy store', () => {
  const t = tmp();
  try {
    for (const [name, image] of [['v2', specV2(STORE, PASS)], ['legacy', specLegacy(STORE, PASS)]]) {
      const f = path.join(t.d, `${name}.enc`);
      writeFileSync(f, image);
      const r = spawnSync(process.execPath, [path.join(HERE, 'cred_store.mjs'), 'pool', 'mikrotik-ssh', '--reveal'], {
        encoding: 'utf8', env: { PATH: process.env.PATH, NOC_CREDENTIALS_ENC: f, NOC_MCP_PASSPHRASE: PASS, NOC_DOTENV: path.join(t.d, 'none') },
      });
      assert.equal(r.status, 0, `${name}: ${r.stderr}`);
      assert.equal(JSON.parse(r.stdout)[0].password, SECRET);
      const wrong = spawnSync(process.execPath, [path.join(HERE, 'cred_store.mjs'), 'pool', 'mikrotik-ssh', '--reveal'], {
        encoding: 'utf8', env: { PATH: process.env.PATH, NOC_CREDENTIALS_ENC: f, NOC_MCP_PASSPHRASE: 'wrong', NOC_DOTENV: path.join(t.d, 'none') }, // secret-scan:allow(fake credential or address fixture for an offline test)
      });
      assert.equal(wrong.status, 3);
      assert.ok(!wrong.stderr.includes(PASS) && !wrong.stdout.includes(SECRET));
    }
  } finally { t.cleanup(); }
});
