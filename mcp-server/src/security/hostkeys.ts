/**
 * SSH host-key pinning (and TLS certificate observation) for device connections.
 *
 * Audit finding: no hostVerifier was configured, so ssh2 accepted any host key
 * and a man-in-the-middle on the management network could harvest every
 * credential the server tries.
 *
 * NOC_SSH_HOSTKEY_MODE:
 *   off    no verification at all (previous behavior).
 *   warn   DEFAULT. Record the SHA256 fingerprint of every host:port on first
 *          sight and audit-log NEW and CHANGED keys. NEVER blocks.
 *   tofu   trust on first use: record unknown keys, BLOCK when a known key changes.
 *   strict BLOCK unknown and changed keys (pre-seed known_hosts.json by running
 *          in warn/tofu first, or by editing the file).
 *
 * Why `warn` is the default: this fleet is full of radios that are factory
 * reset and reprogrammed by the reprogram-device / program-new-device workflows;
 * a legitimate reset regenerates the dropbear/RouterOS host key, so blocking on
 * change by default would break exactly the workflows that touch devices most.
 * In warn mode a changed key is accepted, the stored fingerprint is updated,
 * the old one is kept in `history`, and an audit event records the change so an
 * unexpected rotation is visible. Move to tofu/strict once the fleet is stable.
 * To accept a legitimately changed key under tofu/strict, delete that host's
 * entry from config/known_hosts.json (or temporarily run in warn).
 *
 * Store: mcp-server/config/known_hosts.json (override NOC_KNOWN_HOSTS_FILE),
 * mode 0600, written atomically (temp file + rename). The file is gitignored.
 *
 * TLS (airOS HTTPS, self-signed): certificate fingerprints are recorded/audited
 * the same way but only OBSERVED, never blocked, because the airOS client first
 * sends HTTP Basic credentials over plain HTTP:80 before any redirect, so a
 * TLS pin would not protect them. See the report's known gaps.
 */
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';

export type HostKeyMode = 'off' | 'warn' | 'tofu' | 'strict';
export type HostKeyProtocol = 'ssh' | 'tls';

export interface HostKeyEvent {
  event: 'new' | 'changed' | 'blocked' | 'store_error';
  protocol: HostKeyProtocol;
  host: string;
  port: number;
  fingerprint: string;
  previous?: string;
  mode: HostKeyMode;
  blocked: boolean;
  detail?: string;
}

type Sink = (event: HostKeyEvent) => void;
let sink: Sink | null = null;

/** server.ts installs an audit-log sink; without one events go to stderr. */
export function setHostKeyEventSink(fn: Sink | null): void {
  sink = fn;
}

function emit(event: HostKeyEvent): void {
  try {
    if (sink) sink(event);
    else console.error(`[hostkey] ${JSON.stringify(event)}`);
  } catch { /* auditing must never break a connection */ }
}

export function hostKeyMode(env: NodeJS.ProcessEnv = process.env): HostKeyMode {
  const raw = env.NOC_SSH_HOSTKEY_MODE?.trim().toLowerCase();
  if (!raw) return 'warn';
  if (raw === 'off' || raw === 'warn' || raw === 'tofu' || raw === 'strict') return raw;
  // A typo must not silently downgrade a security setting: fail closed.
  console.error(`[hostkey] unknown NOC_SSH_HOSTKEY_MODE "${raw.slice(0, 20)}"; treating as "strict"`);
  return 'strict';
}

export function knownHostsPath(env: NodeJS.ProcessEnv = process.env): string {
  const configured = env.NOC_KNOWN_HOSTS_FILE?.trim();
  if (configured) return resolve(configured);
  // src/security/ and dist/security/ are both two levels below mcp-server/.
  return join(resolve(dirname(fileURLToPath(import.meta.url)), '..', '..'), 'config', 'known_hosts.json');
}

interface KnownHostEntry {
  fingerprint: string;
  firstSeen: string;
  lastSeen: string;
  history: string[];
}
interface KnownHostsFile {
  version: 1;
  hosts: Record<string, KnownHostEntry>;
}

class StoreError extends Error {}

function loadStore(path: string): KnownHostsFile {
  if (!existsSync(path)) return { version: 1, hosts: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    throw new StoreError('known_hosts.json is unreadable or not valid JSON');
  }
  const obj = parsed as Partial<KnownHostsFile> | null;
  if (!obj || obj.version !== 1 || typeof obj.hosts !== 'object' || obj.hosts === null || Array.isArray(obj.hosts)) {
    throw new StoreError('known_hosts.json has an unexpected structure');
  }
  return { version: 1, hosts: obj.hosts };
}

function saveStore(path: string, data: KnownHostsFile): void {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`;
  writeFileSync(tmp, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 });
  chmodSync(tmp, 0o600);
  renameSync(tmp, path); // atomic on the same filesystem
  chmodSync(path, 0o600);
}

export interface HostKeyDecision {
  allow: boolean;
  event?: HostKeyEvent['event'];
}

/** Core decision function (exported for tests). Fingerprint format: SHA256:<base64>. */
export function checkFingerprint(
  protocol: HostKeyProtocol,
  host: string,
  port: number,
  fingerprint: string,
  env: NodeJS.ProcessEnv = process.env,
  now: () => Date = () => new Date(),
): HostKeyDecision {
  const mode = hostKeyMode(env);
  if (mode === 'off') return { allow: true };
  // TLS is observe-only: never block regardless of mode (see file header).
  const enforcing = protocol === 'ssh';
  const path = knownHostsPath(env);
  const key = `${protocol}:${host.toLowerCase()}:${port}`;
  const base = { protocol, host, port, fingerprint, mode } as const;
  let store: KnownHostsFile;
  try {
    store = loadStore(path);
  } catch (e) {
    const detail = e instanceof Error ? e.message : 'store error';
    const block = enforcing && (mode === 'tofu' || mode === 'strict');
    emit({ ...base, event: 'store_error', blocked: block, detail });
    return { allow: !block, event: 'store_error' };
  }
  const entry = Object.prototype.hasOwnProperty.call(store.hosts, key) ? store.hosts[key] : undefined;
  const stamp = now().toISOString();

  if (entry && entry.fingerprint === fingerprint) return { allow: true };

  if (!entry) {
    if (enforcing && mode === 'strict') {
      emit({ ...base, event: 'blocked', blocked: true, detail: 'unknown host key (strict mode)' });
      return { allow: false, event: 'blocked' };
    }
    store.hosts[key] = { fingerprint, firstSeen: stamp, lastSeen: stamp, history: [] };
    try {
      saveStore(path, store);
    } catch (e) {
      const block = enforcing && mode === 'tofu';
      emit({ ...base, event: 'store_error', blocked: block, detail: e instanceof Error ? e.message : 'write failed' });
      return { allow: !block, event: 'store_error' };
    }
    emit({ ...base, event: 'new', blocked: false });
    return { allow: true, event: 'new' };
  }

  // Known host, different key.
  if (enforcing && (mode === 'tofu' || mode === 'strict')) {
    emit({ ...base, event: 'blocked', previous: entry.fingerprint, blocked: true, detail: 'host key changed' });
    return { allow: false, event: 'blocked' };
  }
  const updated: KnownHostEntry = {
    fingerprint,
    firstSeen: entry.firstSeen,
    lastSeen: stamp,
    history: [...(entry.history ?? []), entry.fingerprint].slice(-5),
  };
  store.hosts[key] = updated;
  try { saveStore(path, store); } catch { /* the change is still audited below */ }
  emit({ ...base, event: 'changed', previous: entry.fingerprint, blocked: false });
  return { allow: true, event: 'changed' };
}

/** ssh2 reports sha256 hashes as lowercase hex; OpenSSH shows SHA256:<base64>. */
export function hexToSshFingerprint(hex: string): string {
  return `SHA256:${Buffer.from(hex, 'hex').toString('base64').replace(/=+$/, '')}`;
}

/** Node reports TLS fingerprints as "AB:CD:..." (sha256). */
export function tlsFingerprint(fingerprint256: string): string {
  return hexToSshFingerprint(fingerprint256.replace(/:/g, ''));
}

/**
 * Spread into ssh2 `connect()` options. Returns {} in `off` mode so the
 * connection is configured exactly as before.
 */
export function sshHostKeyOptions(host: string, port: number): { hostHash?: string; hostVerifier?: (hashedKey: string) => boolean } {
  if (hostKeyMode() === 'off') return {};
  return {
    hostHash: 'sha256',
    hostVerifier: (hashedKey: string): boolean => checkFingerprint('ssh', host, port, hexToSshFingerprint(hashedKey)).allow,
  };
}

/** Observe a TLS peer certificate fingerprint (never blocks). */
export function observeTlsFingerprint(host: string, port: number, fingerprint256: string | undefined): void {
  if (!fingerprint256) return;
  try { checkFingerprint('tls', host, port, tlsFingerprint(fingerprint256)); } catch { /* observation only */ }
}
