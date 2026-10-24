#!/usr/bin/env node
// Deterministic Oxidized backup-config fetch for the agent.
// Usage: node scripts/oxidized_fetch.mjs <device-ip> [--out /tmp/name.cfg]
// Prints only safe identity lines; the full file (which contains secrets)
// is written to --out for operator handling. Never paste the file to chat.
//
// --out safety: the path is resolved (path.resolve), its directory is
// realpath'd, and the final location must sit strictly INSIDE /tmp (or the OS
// temp dir) by a separator-aware prefix check. A symlinked directory that
// points elsewhere, "../" tricks, and a sibling like /tmpevil/ are all refused
// (the previous check was `out.startsWith('/tmp/')` on the raw string). The file
// itself is opened with O_NOFOLLOW + mode 0600, so a pre-planted symlink at the
// target cannot redirect the write.
import { closeSync, constants, fchmodSync, fstatSync, lstatSync, openSync, readFileSync, realpathSync, writeSync } from 'node:fs';
import { isIP } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SECRET_KEYS = [/psk/i, /password/i, /community/i, /unms\.uri/i, /snmp/i];

/** Roots a config dump may be written under (realpath'd, deduped). */
export function allowedOutRoots() {
  const roots = new Set();
  for (const r of ['/tmp', os.tmpdir()]) {
    try { roots.add(realpathSync(r)); } catch { /* absent on this platform */ }
  }
  return [...roots];
}

/**
 * Validate --out. Returns the real absolute target path, or throws Error.
 * `roots` is injectable for tests.
 */
export function safeOutPath(out, roots = allowedOutRoots()) {
  if (typeof out !== 'string' || !out || out.includes('\0')) throw new Error('--out must be a non-empty path');
  const abs = path.resolve(out);
  const base = path.basename(abs);
  if (!base || base === '.' || base === '..') throw new Error('--out must name a file');
  let realDir;
  try { realDir = realpathSync(path.dirname(abs)); } catch { throw new Error('--out directory does not exist'); }
  const target = path.join(realDir, base);
  const inside = roots.some((root) => target.startsWith(root.endsWith(path.sep) ? root : root + path.sep));
  if (!inside) throw new Error(`--out must be under ${roots.join(' or ') || '/tmp'} (resolved to ${target})`);
  try {
    if (lstatSync(target).isSymbolicLink()) throw new Error('--out must not be a symlink');
  } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }
  return target;
}

export function defaultOutPath(ip) {
  return `/tmp/oxidized-${ip.replaceAll('.', '-')}.cfg`;
}

/** Oxidized base URL: env first, else the site-pack temp-bgp config; '' if neither. */
export function resolveOxidizedBase(env = process.env, readConfig = () => readFileSync(new URL('../site-pack/topology/temp-bgp-config.json', import.meta.url), 'utf8')) {
  let base = (env.OXIDIZED_BASE_URL || '').trim();
  if (!base) {
    try { base = String(JSON.parse(readConfig()).oxidized_base_url || '').trim(); } catch { base = ''; }
  }
  base = base.replace(/\/+$/, '');
  if (base && !/^https?:\/\/[^/\s]+/i.test(base)) throw new Error('OXIDIZED_BASE_URL must be an http(s) URL');
  return base;
}

/** Write 0600 through an O_NOFOLLOW descriptor. */
export function writeSecretFile(target, text) {
  const flags = constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC | (constants.O_NOFOLLOW || 0);
  const fd = openSync(target, flags, 0o600);
  try {
    if (!fstatSync(fd).isFile()) throw new Error('--out is not a regular file');
    fchmodSync(fd, 0o600);
    writeSync(fd, text);
  } finally {
    closeSync(fd);
  }
}

async function main() {
  const OXIDIZED_BASE = (() => {
    try { return resolveOxidizedBase(); } catch (e) { console.error(`oxidized_fetch: ${e.message}`); process.exit(2); }
  })();
  if (!OXIDIZED_BASE) {
    console.error('oxidized_fetch: set OXIDIZED_BASE_URL (or oxidized_base_url in site-pack/topology/temp-bgp-config.json)');
    process.exit(2);
  }
  const args = process.argv.slice(2).filter(a => !a.startsWith('--'));
  const outIdx = process.argv.indexOf('--out');
  const ip = args[0];
  if (!ip || isIP(ip) !== 4) {
    console.error('Usage: node scripts/oxidized_fetch.mjs <device-ipv4> [--out /tmp/name.cfg]');
    process.exit(1);
  }
  const requested = outIdx >= 0 && process.argv[outIdx + 1] ? process.argv[outIdx + 1] : defaultOutPath(ip);
  let out;
  try { out = safeOutPath(requested); } catch (e) { console.error(`Refusing: ${e.message}`); process.exit(1); }

  const res = await fetch(`${OXIDIZED_BASE}/node/fetch/${encodeURIComponent(ip)}`, { signal: AbortSignal.timeout(30000) });
  if (!res.ok) { console.error(`Oxidized fetch failed: HTTP ${res.status} for ${ip}`); process.exit(1); }
  const text = await res.text();
  if (!/^wireless\.1\.ssid=|^board\.name=/m.test(text)) {
    console.error(`Oxidized response for ${ip} does not look like an airOS backup config (${text.length} bytes)`);
    process.exit(1);
  }
  try { writeSecretFile(out, text); } catch (e) { console.error(`Refusing: cannot write ${out}: ${e.message}`); process.exit(1); }
  const get = (re) => (text.match(re)?.[1] ?? '(absent)').trim();
  const summary = {
    ip,
    bytes: text.length,
    file: out,
    board: get(/^#?\s*board\.name=(.+)$/m),
    hostname: get(/^resolv\.host\.1\.name=(.+)$/m),
    ssid: get(/^wireless\.1\.ssid=(.+)$/m),
    mode: get(/^radio\.1\.mode=(.+)$/m),
    freq: get(/^radio\.1\.freq=(.+)$/m),
    chanbw: get(/^radio\.1\.chanbw=(.+)$/m),
    mgmt: get(/^netconf\.3\.ip=(.+)$/m),
    secretsPresent: SECRET_KEYS.some((re) => re.test(text)),
  };
  console.log(JSON.stringify(summary, null, 2));
  console.log('Full config saved owner-only. It contains secrets: never paste it to chat/tickets; extract only needed non-secret lines.');
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) await main();
