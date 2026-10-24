import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { CredentialStore, STORE_MAGIC_V2, detectStoreFormat, encryptCredentialStore } from '../src/credentials/store.js';
import { CredentialStoreSchema } from '../src/credentials/schema.js';
import { mkdirSync, rmSync, writeFileSync } from 'fs';
import { join } from 'path';
import { randomBytes, createCipheriv, createDecipheriv, createHash } from 'crypto';

const TEST_DIR = '/tmp/noc-mcp-test-creds';

function encryptData(data: string, passphrase: string): Buffer {
  const key = createHash('sha256').update(passphrase).digest();
  const iv = randomBytes(16);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  const encrypted = Buffer.concat([cipher.update(data, 'utf-8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, encrypted]);
}

describe('CredentialStore', () => {
  beforeEach(() => {
    mkdirSync(TEST_DIR, { recursive: true });
  });

  afterEach(() => {
    rmSync(TEST_DIR, { recursive: true, force: true });
  });

  it('loads and decrypts credential store', async () => {
    const plaintext = JSON.stringify({
      version: 1,
      deviceTypes: [{
        deviceType: 'mikrotik-ssh',
        credentials: [{
          name: 'admin',
          username: 'admin',
          password: 'secret123',
          priority: 0,
          tags: [],
        }],
      }],
    });

    const encrypted = encryptData(plaintext, 'test-passphrase');
    const credPath = join(TEST_DIR, 'credentials.enc');
    writeFileSync(credPath, encrypted);

    const store = new CredentialStore(credPath, 'test-passphrase');
    const data = await store.load();

    expect(data.version).toBe(1);
    expect(data.deviceTypes).toHaveLength(1);
    expect(data.deviceTypes[0].deviceType).toBe('mikrotik-ssh');
    expect(data.deviceTypes[0].credentials[0].password).toBe('secret123');
  });

  it('rejects wrong passphrase', async () => {
    const plaintext = JSON.stringify({ version: 1, deviceTypes: [] });
    const encrypted = encryptData(plaintext, 'correct-passphrase');
    writeFileSync(join(TEST_DIR, 'credentials.enc'), encrypted);

    const store = new CredentialStore(join(TEST_DIR, 'credentials.enc'), 'wrong-passphrase');
    await expect(store.load()).rejects.toThrow();
  });

  it('rejects tampered ciphertext', async () => {
    const plaintext = JSON.stringify({ version: 1, deviceTypes: [] });
    const encrypted = encryptData(plaintext, 'test-passphrase');
    encrypted[32] ^= 0xff;
    writeFileSync(join(TEST_DIR, 'credentials.enc'), encrypted);

    const store = new CredentialStore(join(TEST_DIR, 'credentials.enc'), 'test-passphrase');
    await expect(store.load()).rejects.toThrow();
  });

  it('validates schema on load', async () => {
    const plaintext = JSON.stringify({ version: 1, deviceTypes: 'not-an-array' });
    const encrypted = encryptData(plaintext, 'test-passphrase');
    writeFileSync(join(TEST_DIR, 'credentials.enc'), encrypted);

    const store = new CredentialStore(join(TEST_DIR, 'credentials.enc'), 'test-passphrase');
    await expect(store.load()).rejects.toThrow();
  });
});

// A real file produced once by the ORIGINAL (pre-v2) algorithm with a fixed IV
// and a fake passphrase. It must keep decrypting forever: production's
// credentials.enc is in this format.
const LEGACY_FIXTURE_HEX = '000102030405060708090a0b0c0d0e0f58024f83a7f3f0fed144e171aafbe6eb966d2c6ea74af42bee2bd09c13206ea3dbdbc33569a449d12e216a54494bca1e44dabcfdda968f09e1bceb844e8a63f41b728365a290fdadeeaffb5cef7c3f079b342f863b03a1c39a51bd502272c82a8230fcb5048db0e8078808dd49d5ee29d54eff407e96bb12793a5bc67b058abb8ddd598e519ddf63e714ca58ace71c8276c055776b7286dba2469b9d37283810190c5363b5d1cc92e770101937dca2010a34';

const SAMPLE = CredentialStoreSchema.parse({
  version: 1,
  deviceTypes: [{ deviceType: 'mikrotik-ssh', credentials: [{ name: 'a', username: 'admin', password: 'v2-secret-pw', priority: 0, tags: [] }] }],
});

describe('CredentialStore formats (legacy compatibility + NOCENC2)', () => {
  beforeEach(() => { mkdirSync(TEST_DIR, { recursive: true }); });
  afterEach(() => { rmSync(TEST_DIR, { recursive: true, force: true }); });
  const file = () => join(TEST_DIR, 'credentials.enc');

  it('decrypts a byte-for-byte legacy fixture (production format) unchanged', async () => {
    const bytes = Buffer.from(LEGACY_FIXTURE_HEX, 'hex');
    expect(detectStoreFormat(bytes)).toBe('legacy');
    writeFileSync(file(), bytes);
    const data = await new CredentialStore(file(), 'legacy-fixture-passphrase').load();
    expect(data.deviceTypes[0].credentials[0].password).toBe('fixture-pw-1');
    await expect(new CredentialStore(file(), 'wrong').load()).rejects.toThrow(/Decryption failed/);
  });

  it('encryptCredentialStore({format:"legacy"}) still writes the old layout', async () => {
    const bytes = encryptCredentialStore(SAMPLE, 'pw-legacy', { format: 'legacy' });
    expect(detectStoreFormat(bytes)).toBe('legacy');
    writeFileSync(file(), bytes);
    expect((await new CredentialStore(file(), 'pw-legacy').load()).deviceTypes[0].credentials[0].password).toBe('v2-secret-pw');
    // and it matches the independent reference implementation in this test file
    const key = createHash('sha256').update('pw-legacy').digest();
    const d = createDecipheriv('aes-256-gcm', key, bytes.subarray(0, 16));
    d.setAuthTag(bytes.subarray(16, 32));
    expect(JSON.parse(Buffer.concat([d.update(bytes.subarray(32)), d.final()]).toString()).version).toBe(1);
  });

  it('v2 round trip: magic header, random salt, scrypt key', async () => {
    const a = encryptCredentialStore(SAMPLE, 'pw-v2');
    const b = encryptCredentialStore(SAMPLE, 'pw-v2');
    expect(detectStoreFormat(a)).toBe('v2');
    expect(a.subarray(0, 7).toString('ascii')).toBe('NOCENC2');
    expect(a.subarray(0, 7).equals(STORE_MAGIC_V2)).toBe(true);
    expect(a.subarray(7, 23).equals(b.subarray(7, 23))).toBe(false); // fresh salt every time
    writeFileSync(file(), a);
    const loaded = await new CredentialStore(file(), 'pw-v2').load();
    expect(loaded.deviceTypes[0].credentials[0].password).toBe('v2-secret-pw');
    expect(a.toString('utf8')).not.toContain('v2-secret-pw');
  });

  it('v2: wrong passphrase fails closed', async () => {
    writeFileSync(file(), encryptCredentialStore(SAMPLE, 'pw-v2'));
    await expect(new CredentialStore(file(), 'pw-v3').load()).rejects.toThrow('Decryption failed — wrong passphrase or tampered data');
    await expect(new CredentialStore(file(), '').load()).rejects.toThrow(/Decryption failed/);
  });

  it('v2: any tampering fails closed (ciphertext, tag, iv, salt, truncation, magic swap)', async () => {
    const good = encryptCredentialStore(SAMPLE, 'pw-v2');
    const flips: Array<[string, number]> = [['salt', 8], ['iv', 25], ['tag', 40], ['ciphertext', 60], ['last byte', good.length - 1]];
    for (const [label, offset] of flips) {
      const bad = Buffer.from(good);
      bad[offset] ^= 0x01;
      writeFileSync(file(), bad);
      await expect(new CredentialStore(file(), 'pw-v2').load(), label).rejects.toThrow(/Decryption failed/);
    }
    writeFileSync(file(), good.subarray(0, 30));
    await expect(new CredentialStore(file(), 'pw-v2').load(), 'truncated').rejects.toThrow(/Decryption failed/);
    const downgraded = Buffer.from(good);
    downgraded[0] ^= 0x01; // magic no longer matches -> parsed as legacy -> must not decrypt
    writeFileSync(file(), downgraded);
    await expect(new CredentialStore(file(), 'pw-v2').load(), 'magic swap').rejects.toThrow(/Decryption failed/);
  });

  it('a v2 file is not decryptable with the legacy derivation (no unsalted sha256 shortcut)', () => {
    const v2 = encryptCredentialStore(SAMPLE, 'pw-v2');
    const key = createHash('sha256').update('pw-v2').digest();
    const attempt = () => {
      const d = createDecipheriv('aes-256-gcm', key, v2.subarray(0, 16));
      d.setAuthTag(v2.subarray(16, 32));
      return Buffer.concat([d.update(v2.subarray(32)), d.final()]);
    };
    expect(attempt).toThrow();
  });
});

