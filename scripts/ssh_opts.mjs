// ssh_opts.mjs - host-key policy shared by the node ops scripts (mirrors scripts/ssh_common.py).
//
//   SSH_HOSTKEY_CHECKING   accept-new (default) | yes | no
//       accept-new  trust a host on first contact, refuse if its key later CHANGES
//       yes         only hosts already known
//       no          legacy: verify nothing, remember nothing (prints a warning)
//   SSH_KNOWN_HOSTS_FILE   default ${XDG_CACHE_HOME:-~/.cache}/nocagent/known_hosts (dir 0700)
//
// For the `ssh` binary, sshOptionArgs() returns the -o options. For the ssh2 library
// (mt_mactelnet.mjs) makeHostVerifier() keeps SHA-256 fingerprints in ssh2_hostkeys.json
// next to the known_hosts file with the same accept-new / yes / no semantics.
import { closeSync, constants, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, writeSync } from 'node:fs';
import os from 'node:os';
import { dirname, join } from 'node:path';
import { randomBytes } from 'node:crypto';

const MODES = ['accept-new', 'yes', 'no'];
let warned = false;

export function hostkeyMode(env = process.env) {
  const mode = (env.SSH_HOSTKEY_CHECKING || '').trim().toLowerCase() || 'accept-new';
  if (!MODES.includes(mode)) throw new Error(`SSH_HOSTKEY_CHECKING=${mode} must be one of ${MODES.join(', ')}`);
  return mode;
}

export function knownHostsFile(env = process.env, { create = true } = {}) {
  const override = (env.SSH_KNOWN_HOSTS_FILE || '').trim();
  const cache = (env.XDG_CACHE_HOME || '').trim() || join(os.homedir(), '.cache');
  const file = override || join(cache, 'nocagent', 'known_hosts');
  if (create) mkdirSync(dirname(file), { recursive: true, mode: 0o700 });
  return file;
}

function warnOnce() {
  if (!warned) { console.error('WARNING: SSH_HOSTKEY_CHECKING=no - SSH host keys are NOT verified'); warned = true; }
}

/** -o options for the ssh binary. */
export function sshOptionArgs({ legacyAlgos = false, connectTimeout = 10, env = process.env } = {}) {
  const mode = hostkeyMode(env);
  const args = [];
  if (mode === 'no') {
    warnOnce();
    args.push('-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null');
  } else {
    args.push('-o', `StrictHostKeyChecking=${mode}`, '-o', `UserKnownHostsFile=${knownHostsFile(env)}`);
  }
  args.push('-o', `ConnectTimeout=${connectTimeout}`, '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no', '-o', 'LogLevel=ERROR');
  if (legacyAlgos) args.push('-o', 'HostKeyAlgorithms=+ssh-rsa,ssh-dss', '-o', 'PubkeyAcceptedAlgorithms=+ssh-rsa');
  return args;
}

/** Pure decision for one host key. known = {host: sha256hex}. */
export function checkHostKey({ mode, known, host, fingerprint }) {
  if (mode === 'no') return { ok: true, remember: false };
  const have = known[host];
  if (have === fingerprint) return { ok: true, remember: false };
  if (have) return { ok: false, remember: false, reason: `host key for ${host} CHANGED (expected ${have.slice(0, 16)}..., got ${fingerprint.slice(0, 16)}...)` };
  if (mode === 'yes') return { ok: false, remember: false, reason: `host ${host} is not in the known-hosts file and SSH_HOSTKEY_CHECKING=yes` };
  return { ok: true, remember: true };
}

function loadKnown(file) {
  try { return JSON.parse(readFileSync(file, 'utf8')) || {}; } catch { return {}; }
}

function saveKnown(file, data) {
  const tmp = `${file}.${process.pid}.${randomBytes(6).toString('hex')}.tmp`;
  const fd = openSync(tmp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
  try { writeSync(fd, JSON.stringify(data, null, 2)); fsyncSync(fd); } finally { closeSync(fd); }
  renameSync(tmp, file);
}

/**
 * ssh2 `hostVerifier` for `host` (use with `hostHash: 'sha256'`). Never throws:
 * a refusal is logged (fingerprints only) and returns false.
 */
export function makeHostVerifier(host, { env = process.env, log = console.error } = {}) {
  return (fingerprint) => {
    let mode;
    try { mode = hostkeyMode(env); } catch (e) { log(`ssh host key: ${e.message}`); return false; }
    if (mode === 'no') { warnOnce(); return true; }
    const file = join(dirname(knownHostsFile(env)), 'ssh2_hostkeys.json');
    const known = existsSync(file) ? loadKnown(file) : {};
    const verdict = checkHostKey({ mode, known, host, fingerprint: String(fingerprint) });
    if (!verdict.ok) { log(`ssh host key REFUSED: ${verdict.reason}. If this device was reset/replaced, remove its entry from ${file}.`); return false; }
    if (verdict.remember) {
      known[host] = String(fingerprint);
      try { saveKnown(file, known); } catch (e) { log(`ssh host key: could not record key (${e.message})`); }
    }
    return true;
  };
}
