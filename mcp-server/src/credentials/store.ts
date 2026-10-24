import { readFileSync } from 'fs';
import { createDecipheriv, createHash, randomBytes, createCipheriv, scrypt, scryptSync, type ScryptOptions } from 'crypto';
import { promisify } from 'util';
import { CredentialStoreSchema, type CredentialStore as CredentialStoreData } from './schema.js';

/**
 * Credential store file formats.
 *
 * legacy  (production's existing credentials.enc, still readable BYTE FOR BYTE):
 *           iv(16) | gcm tag(16) | ciphertext
 *         key = sha256(passphrase) -- no salt, no work factor, no version.
 *
 * NOCENC2 (written only by an explicit `npm run encrypt`):
 *           "NOCENC2"(7) | salt(16) | iv(12) | gcm tag(16) | ciphertext
 *         key = scrypt(passphrase, salt, N=2^15, r=8, p=1) -> 32 bytes.
 *         The magic + salt are bound into the GCM tag as additional
 *         authenticated data, so the header cannot be swapped or downgraded.
 *
 * Detection is by the 7-byte magic. A legacy file starts with a random IV, so
 * it matches the magic with probability 2^-56. Both formats fail closed with
 * the same generic error (wrong passphrase and tampering are indistinguishable).
 *
 * NOTE: scripts/cred_store.mjs / provision_exact_binding.mjs duplicate the
 * LEGACY derivation and cannot read NOCENC2 until they are taught the format
 * (or the store is written with `npm run encrypt -- --legacy`).
 */
export const STORE_MAGIC_V2 = Buffer.from('NOCENC2', 'ascii');
const SALT_LEN = 16;
const IV_LEN_V2 = 12;
const TAG_LEN = 16;
const SCRYPT_PARAMS: ScryptOptions = { N: 1 << 15, r: 8, p: 1, maxmem: 64 * 1024 * 1024 };
const KEY_LEN = 32;
const DECRYPT_FAILED = 'Decryption failed — wrong passphrase or tampered data';

export type StoreFormat = 'v2' | 'legacy';

const scryptAsync = promisify(scrypt) as (password: string, salt: Buffer, keylen: number, options: ScryptOptions) => Promise<Buffer>;

export function detectStoreFormat(file: Buffer): StoreFormat {
  return file.length >= STORE_MAGIC_V2.length && file.subarray(0, STORE_MAGIC_V2.length).equals(STORE_MAGIC_V2) ? 'v2' : 'legacy';
}

function decryptLegacy(encrypted: Buffer, passphrase: string): Buffer {
  // Derive key from passphrase (legacy: unsalted sha256)
  const key = createHash('sha256').update(passphrase).digest();
  // Extract IV (16 bytes), auth tag (16 bytes), and ciphertext
  const iv = encrypted.subarray(0, 16);
  const tag = encrypted.subarray(16, 32);
  const ciphertext = encrypted.subarray(32);
  const decipher = createDecipheriv('aes-256-gcm', key, iv);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
}

async function decryptV2(encrypted: Buffer, passphrase: string): Promise<Buffer> {
  const headerLen = STORE_MAGIC_V2.length + SALT_LEN;
  if (encrypted.length < headerLen + IV_LEN_V2 + TAG_LEN) throw new Error('truncated');
  const header = encrypted.subarray(0, headerLen);
  const salt = encrypted.subarray(STORE_MAGIC_V2.length, headerLen);
  const iv = encrypted.subarray(headerLen, headerLen + IV_LEN_V2);
  const tag = encrypted.subarray(headerLen + IV_LEN_V2, headerLen + IV_LEN_V2 + TAG_LEN);
  const ciphertext = encrypted.subarray(headerLen + IV_LEN_V2 + TAG_LEN);
  const key = await scryptAsync(passphrase, Buffer.from(salt), KEY_LEN, SCRYPT_PARAMS);
  const decipher = createDecipheriv('aes-256-gcm', key, iv);
  decipher.setAAD(header);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
}

export class CredentialStore {
  private path: string;
  private passphrase: string;
  private cached: CredentialStoreData | null = null;

  constructor(path: string, passphrase: string) {
    this.path = path;
    this.passphrase = passphrase;
  }

  async load(): Promise<CredentialStoreData> {
    if (this.cached) return this.cached;

    const encrypted = readFileSync(this.path);

    let plaintext: Buffer;
    try {
      plaintext = detectStoreFormat(encrypted) === 'v2'
        ? await decryptV2(encrypted, this.passphrase)
        : decryptLegacy(encrypted, this.passphrase);
    } catch {
      throw new Error(DECRYPT_FAILED);
    }

    const data = JSON.parse(plaintext.toString('utf-8'));
    const validated = CredentialStoreSchema.parse(data);

    this.cached = validated;
    return validated;
  }

  invalidate(): void {
    this.cached = null;
  }
}

// CLI tool: encrypt-credentials.ts uses this to encrypt plaintext JSON.
// Default format is NOCENC2; pass { format: 'legacy' } only for tools that
// cannot read it yet (and for tests that prove legacy files stay readable).
export function encryptCredentialStore(data: CredentialStoreData, passphrase: string, opts: { format?: StoreFormat } = {}): Buffer {
  const plaintext = JSON.stringify(data, null, 2);
  if (opts.format === 'legacy') {
    const key = createHash('sha256').update(passphrase).digest();
    const iv = randomBytes(16);
    const cipher = createCipheriv('aes-256-gcm', key, iv);
    const encrypted = Buffer.concat([cipher.update(plaintext, 'utf-8'), cipher.final()]);
    const tag = cipher.getAuthTag();
    return Buffer.concat([iv, tag, encrypted]);
  }
  const salt = randomBytes(SALT_LEN);
  const iv = randomBytes(IV_LEN_V2);
  const key = scryptSync(passphrase, salt, KEY_LEN, SCRYPT_PARAMS);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  cipher.setAAD(Buffer.concat([STORE_MAGIC_V2, salt]));
  const encrypted = Buffer.concat([cipher.update(plaintext, 'utf-8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([STORE_MAGIC_V2, salt, iv, tag, encrypted]);
}
