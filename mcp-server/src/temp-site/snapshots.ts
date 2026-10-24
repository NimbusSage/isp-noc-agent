/**
 * Private storage for temp-site pre-apply snapshots (airOS system.cfg) and
 * boot-id tripwire records.
 *
 * Audit finding: snapshots lived in the predictable, world-reachable
 * /tmp/noc-temp-site-snapshots. Anyone able to pre-create that directory (or a
 * symlink in it) could read device configs or plant a snapshot that
 * temp-radio-rollback would later write onto a radio.
 *
 * Why not mkdtemp: a rollback is a LATER tool call, frequently a different
 * process (every tool-cli invocation is its own process; the MCP server can be
 * restarted between apply and rollback), so a random per-process directory
 * would lose the snapshot. Instead the directory is stable but private:
 *   - TEMP_SITE_SNAPSHOT_DIR if set, else <mcp-server>/state/temp-site-snapshots
 *     (inside the install, not in shared /tmp);
 *   - created 0700, lstat-verified (not a symlink, owned by the current user)
 *     and chmod 0700 re-enforced on EVERY use, even if it already existed;
 *   - files are written 0600.
 * Snapshots written by older versions in /tmp/noc-temp-site-snapshots are still
 * readable (so an in-flight incident can be rolled back) but only if that
 * directory and the file are owned by the current user and not group/world
 * writable.
 */
import { chmodSync, lstatSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const LEGACY_SNAPSHOT_DIR = '/tmp/noc-temp-site-snapshots';

function mcpRoot(): string {
  // src/temp-site/ (tests) and dist/temp-site/ (runtime) are both two levels below mcp-server/.
  return resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
}

export function snapshotDir(env: NodeJS.ProcessEnv = process.env): string {
  const configured = env.TEMP_SITE_SNAPSHOT_DIR?.trim();
  if (configured) return resolve(configured);
  return join(mcpRoot(), 'state', 'temp-site-snapshots');
}

function currentUid(): number | null {
  return typeof process.getuid === 'function' ? process.getuid() : null;
}

/** Create (if needed) and verify the private directory. Throws if it cannot be trusted. */
export function ensurePrivateDir(dir: string): void {
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const st = lstatSync(dir);
  if (st.isSymbolicLink() || !st.isDirectory()) {
    throw new Error(`Snapshot directory ${dir} is not a plain directory (symlink or file); refusing`);
  }
  const uid = currentUid();
  if (uid !== null && st.uid !== uid) {
    throw new Error(`Snapshot directory ${dir} is not owned by the current user; refusing`);
  }
  chmodSync(dir, 0o700); // enforced even when the directory pre-existed with looser bits
}

/** Stable per-incident snapshot file name (does not reveal the incident ID or host). */
export function snapshotName(incidentId: string, host: string): string {
  return `${createHash('sha256').update(`${incidentId}:${host}`).digest('hex').slice(0, 24)}.cfg`;
}

export function bootIdName(host: string): string {
  return `${host.replace(/[^0-9a-z.-]/gi, '_')}.bootid`;
}

export function writeSnapshotFile(name: string, content: string): string {
  const dir = snapshotDir();
  ensurePrivateDir(dir);
  const file = join(dir, name);
  try {
    if (lstatSync(file).isSymbolicLink()) throw new Error(`Refusing to write through symlink ${file}`);
  } catch (e) {
    if (!(e instanceof Error && 'code' in e && (e as NodeJS.ErrnoException).code === 'ENOENT')) throw e;
  }
  writeFileSync(file, content, { mode: 0o600 });
  chmodSync(file, 0o600);
  return file;
}

function trustedLegacy(path: string, wantDir: boolean): boolean {
  try {
    const st = lstatSync(path);
    if (st.isSymbolicLink()) return false;
    if (wantDir ? !st.isDirectory() : !st.isFile()) return false;
    const uid = currentUid();
    if (uid !== null && st.uid !== uid) return false;
    return (st.mode & 0o022) === 0; // not group/world writable
  } catch {
    return false;
  }
}

/** Returns the file contents or null when absent/untrusted. */
export function readSnapshotFile(name: string, env: NodeJS.ProcessEnv = process.env): string | null {
  const dir = snapshotDir(env);
  ensurePrivateDir(dir); // an untrusted directory is an error, never a silent miss
  try {
    return readFileSync(join(dir, name), 'utf8');
  } catch (e) {
    if (!(e instanceof Error && 'code' in e && (e as NodeJS.ErrnoException).code === 'ENOENT')) throw e;
  }
  if (env.TEMP_SITE_SNAPSHOT_DIR?.trim()) return null;
  const legacyFile = join(LEGACY_SNAPSHOT_DIR, name);
  if (!trustedLegacy(LEGACY_SNAPSHOT_DIR, true) || !trustedLegacy(legacyFile, false)) return null;
  try {
    return readFileSync(legacyFile, 'utf8');
  } catch {
    return null;
  }
}

export function removeSnapshotFile(name: string): void {
  try { unlinkSync(join(snapshotDir(), name)); } catch { /* absent or keep on failure */ }
  if (!process.env.TEMP_SITE_SNAPSHOT_DIR?.trim() && trustedLegacy(join(LEGACY_SNAPSHOT_DIR, name), false)) {
    try { unlinkSync(join(LEGACY_SNAPSHOT_DIR, name)); } catch { /* keep */ }
  }
}
