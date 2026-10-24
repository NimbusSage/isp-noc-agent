#!/usr/bin/env node
// cred_store.mjs - the ONE place the ops scripts read the encrypted MCP credential
// store (mcp-server/config/credentials.enc). Replaces every read of the plaintext
// mcp-server/config/credentials.json, which must not exist on a hardened host.
//
// File formats (single source of truth: mcp-server/src/credentials/store.ts; node:crypto only):
//   legacy  iv(16) | gcm tag(16) | ciphertext        key = sha256(passphrase)
//   NOCENC2 "NOCENC2"(7) | salt(16) | iv(12) | gcm tag(16) | ciphertext
//           key = scrypt(passphrase, salt, N=2^15, r=8, p=1) -> 32 bytes; magic+salt are the GCM AAD
// Detection is by the 7-byte magic. Both formats are READ; a re-written store KEEPS the format
// of the file it replaces (v2 stays v2, legacy stays legacy; a brand-new file is v2), unless a
// caller passes an explicit format. Every failure (wrong passphrase, tampering, truncation)
// raises the same generic "decryption failed" error and prints no key material.
//
// Library use (node scripts):
//   import { loadStore, poolCredentials, writeStoreAtomic } from './cred_store.mjs';
//
// CLI use (python / bash callers; passwords go over a pipe, never argv):
//   node scripts/cred_store.mjs pool <deviceType> [--user admin] --reveal
//     -> stdout: JSON array [{name, username, password, apiToken?, priority}], priority order.
//   Refuses to print to a terminal, and requires --reveal so a stray run cannot dump secrets.
//   Exit: 0 ok, 1 usage, 2 passphrase missing, 3 store unreadable/decrypt failed, 4 no credentials.
//
// The passphrase is NOC_MCP_PASSPHRASE from the environment, else from the .env file
// (first NOC_MCP_PASSPHRASE= line; plain parse, no shell evaluation).
import { createCipheriv, createDecipheriv, createHash, randomBytes, scryptSync } from 'node:crypto';
import {
  chmodSync, closeSync, constants, copyFileSync, existsSync, fsyncSync, openSync, readFileSync, renameSync, unlinkSync, writeSync,
} from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** Store location: NOC_CREDENTIALS_ENC override (tests / relocated installs), else the repo's. */
export function defaultStorePath(root = ROOT, env = process.env) {
  const override = (env.NOC_CREDENTIALS_ENC || '').trim();
  return override ? resolve(override) : resolve(root, 'mcp-server', 'config', 'credentials.enc');
}

/** Value of `name` from .env text (KEY=value, optional export, quotes, trailing ` #` comments). */
export function parseDotenvValue(text, name) {
  for (const raw of String(text).replace(/^﻿/, '').split('\n')) {
    const line = raw.replace(/\r$/, '').trim();
    if (!line || line.startsWith('#')) continue;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!m || m[1] !== name) continue;
    let v = m[2];
    if (v.startsWith('"')) { const e = v.indexOf('"', 1); v = e > 0 ? v.slice(1, e) : v.slice(1); }
    else if (v.startsWith("'")) { const e = v.indexOf("'", 1); v = e > 0 ? v.slice(1, e) : v.slice(1); }
    else v = v.replace(/\s#.*$/, '').trim();
    return v;
  }
  return '';
}

export function resolvePassphrase(env = process.env, root = ROOT) {
  const fromEnv = (env.NOC_MCP_PASSPHRASE || '').trim();
  if (fromEnv) return fromEnv;
  const dotenv = (env.NOC_DOTENV || '').trim() || join(root, '.env');
  try { return parseDotenvValue(readFileSync(dotenv, 'utf8'), 'NOC_MCP_PASSPHRASE').trim(); } catch { return ''; }
}

export const STORE_MAGIC_V2 = Buffer.from('NOCENC2', 'ascii');
const SALT_LEN = 16;
const IV_LEN_V2 = 12;
const IV_LEN_LEGACY = 16;
const TAG_LEN = 16;
const SCRYPT = { N: 1 << 15, r: 8, p: 1, maxmem: 64 * 1024 * 1024 };
const DECRYPT_FAILED = 'credential store decryption failed (wrong NOC_MCP_PASSPHRASE or tampered file)';
export const STORE_FORMATS = ['v2', 'legacy'];

const legacyKey = (passphrase) => createHash('sha256').update(passphrase).digest();
const v2Key = (passphrase, salt) => scryptSync(passphrase, salt, 32, SCRYPT);

/** 'v2' when the file starts with the NOCENC2 magic, else 'legacy'. */
export function detectStoreFormat(buf) {
  return buf.length >= STORE_MAGIC_V2.length && buf.subarray(0, STORE_MAGIC_V2.length).equals(STORE_MAGIC_V2) ? 'v2' : 'legacy';
}

/** Decrypt a store image. Returns { data, format }. Throws the generic failure error. */
export function decryptStoreBuffer(enc, passphrase) {
  const format = detectStoreFormat(enc);
  try {
    let plaintext;
    if (format === 'v2') {
      const headerLen = STORE_MAGIC_V2.length + SALT_LEN;
      if (enc.length < headerLen + IV_LEN_V2 + TAG_LEN) throw new Error('truncated');
      const header = enc.subarray(0, headerLen);
      const salt = enc.subarray(STORE_MAGIC_V2.length, headerLen);
      const iv = enc.subarray(headerLen, headerLen + IV_LEN_V2);
      const tag = enc.subarray(headerLen + IV_LEN_V2, headerLen + IV_LEN_V2 + TAG_LEN);
      const d = createDecipheriv('aes-256-gcm', v2Key(passphrase, salt), iv);
      d.setAAD(header);
      d.setAuthTag(tag);
      plaintext = Buffer.concat([d.update(enc.subarray(headerLen + IV_LEN_V2 + TAG_LEN)), d.final()]);
    } else {
      if (enc.length < IV_LEN_LEGACY + TAG_LEN + 1) throw new Error('truncated');
      const d = createDecipheriv('aes-256-gcm', legacyKey(passphrase), enc.subarray(0, IV_LEN_LEGACY));
      d.setAuthTag(enc.subarray(IV_LEN_LEGACY, IV_LEN_LEGACY + TAG_LEN));
      plaintext = Buffer.concat([d.update(enc.subarray(IV_LEN_LEGACY + TAG_LEN)), d.final()]);
    }
    return { data: JSON.parse(plaintext.toString('utf8')), format };
  } catch {
    throw new Error(DECRYPT_FAILED);   // generic on purpose: no oracle for wrong-passphrase vs tamper
  }
}

/** Read + decrypt a store file. Returns { data, format }. */
export function readStore(file, passphrase) {
  return decryptStoreBuffer(readFileSync(file), passphrase);
}

export function decryptStore(file, passphrase) {
  return readStore(file, passphrase).data;
}

/** Encrypt to a store image in `format` (default v2). */
export function encryptStore(data, passphrase, format = 'v2') {
  if (!STORE_FORMATS.includes(format)) throw new Error(`unknown store format '${format}' (use v2 or legacy)`);
  const plaintext = JSON.stringify(data, null, 2);
  if (format === 'legacy') {
    const iv = randomBytes(IV_LEN_LEGACY);
    const c = createCipheriv('aes-256-gcm', legacyKey(passphrase), iv);
    const ct = Buffer.concat([c.update(plaintext, 'utf8'), c.final()]);
    return Buffer.concat([iv, c.getAuthTag(), ct]);
  }
  const salt = randomBytes(SALT_LEN);
  const iv = randomBytes(IV_LEN_V2);
  const c = createCipheriv('aes-256-gcm', v2Key(passphrase, salt), iv);
  c.setAAD(Buffer.concat([STORE_MAGIC_V2, salt]));
  const ct = Buffer.concat([c.update(plaintext, 'utf8'), c.final()]);
  return Buffer.concat([STORE_MAGIC_V2, salt, iv, c.getAuthTag(), ct]);
}

export function loadStore({ file = defaultStorePath(), passphrase = resolvePassphrase() } = {}) {
  if (!passphrase) throw Object.assign(new Error('NOC_MCP_PASSPHRASE is not set (environment or .env)'), { code: 'NO_PASSPHRASE' });
  if (!existsSync(file)) throw new Error(`credential store not found: ${file}`);
  return decryptStore(file, passphrase);
}

/** Credentials of one device type in priority order (highest first). */
export function poolCredentials(store, deviceType) {
  const group = (store.deviceTypes || []).find((g) => g.deviceType === deviceType);
  return [...(group?.credentials || [])].sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0));
}

/**
 * Atomic, durable store update: backup copy (credentials.enc.bak-<ts>, 0600), then a fresh
 * 0600 sibling (O_EXCL) -> fsync -> rename over the target. A crash can never leave a
 * half-written store, and nothing secret is logged.
 *
 * Format: `opts.format` if given ('v2' | 'legacy'), else the format of the file being
 * replaced (v2 stays v2, legacy stays legacy), else v2 for a brand-new file.
 */
export function writeStoreAtomic(file, data, passphrase, opts = {}) {
  let format = opts.format;
  if (existsSync(file)) {
    if (!format) format = detectStoreFormat(readFileSync(file));
    const bak = `${file}.bak-${Date.now()}`;
    copyFileSync(file, bak);
    chmodSync(bak, 0o600);
  }
  const image = encryptStore(data, passphrase, format || 'v2');   // validates `format` before touching disk
  const tmp = `${file}.${process.pid}.${randomBytes(6).toString('hex')}.tmp`;
  const fd = openSync(tmp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
  try {
    writeSync(fd, image);
    fsyncSync(fd);
  } catch (e) {
    closeSync(fd);
    try { unlinkSync(tmp); } catch { /* best effort */ }
    throw e;
  }
  closeSync(fd);
  chmodSync(tmp, 0o600);
  renameSync(tmp, file);
  return format || 'v2';
}

function cliFail(code, msg) { console.error(`cred_store: ${msg}`); process.exit(code); }

function cli(argv) {
  const [cmd, deviceType] = argv;
  if (cmd !== 'pool' || !deviceType || !/^[a-z0-9-]{1,40}$/.test(deviceType)) {
    cliFail(1, 'usage: cred_store.mjs pool <deviceType> [--user NAME] --reveal');
  }
  if (!argv.includes('--reveal')) cliFail(1, 'refusing to print secrets without --reveal');
  if (process.stdout.isTTY) cliFail(1, 'refusing to print secrets to a terminal; pipe the output to the calling script');
  const ui = argv.indexOf('--user');
  const user = ui >= 0 ? argv[ui + 1] : null;
  let store;
  try { store = loadStore(); } catch (e) { cliFail(e.code === 'NO_PASSPHRASE' ? 2 : 3, e.message); }
  let creds = poolCredentials(store, deviceType);
  if (user) creds = creds.filter((c) => (c.username || c.user) === user);
  if (!creds.length) cliFail(4, `no credentials for device type '${deviceType}'${user ? ` and user '${user}'` : ''} in the store`);
  process.stdout.write(JSON.stringify(creds.map((c) => ({
    name: c.name, username: c.username || c.user, password: c.password ?? '', apiToken: c.apiToken, priority: c.priority ?? 0,
  }))));
}

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) cli(process.argv.slice(2));
