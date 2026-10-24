import { Client } from 'ssh2';
import { join } from 'node:path';
import { sshHostKeyOptions } from '../security/hostkeys.js';
import { bootIdName, readSnapshotFile, snapshotDir, writeSnapshotFile } from './snapshots.js';
import type { DeviceTypeCredential } from '../credentials/schema.js';

/**
 * airOS SSH transport for temp-site radio writes.
 * Edits ONLY allowlisted keys in /tmp/system.cfg via anchored sed, verifies
 * every edit by re-read, applies with the operator-verified airMAX command
 * (/usr/etc/rc.d/rc.softrestart save, confirmed  — no full reboot
 * needed for wireless/mode changes), and restores the snapshot on any
 * missed verification (safe-mode rollback). Secrets never appear in audit.
 *
 * Apply syntax is OPERATOR-VERIFIED; live use still requires the safe-area
 * test (TEMP_SITE_AIROS_TRANSPORT=verified + host allowlist) first.
 */

/* PTP/PTMP PROTOCOL LOCK (operator rule): temp flows NEVER change airMAX
 * PTP/PTMP protocol modes (e.g. ptp_only, ptmp_only, airMAX protocol
 * selection). Those change only when the specific link requires it — e.g.
 * joining a backhaul radio to a customer AP for a temp path — as a separate
 * operator-reviewed step, never as part of a mode/ssid/frequency flip. This
 * allowlist is therefore closed to protocol keys by design; adding one
 * requires the same review as a new executor capability. */
export const ALLOWLISTED_KEYS = [
  'radio.1.mode',
  'radio.1.freq',
  'radio.1.chanbw',
  'wireless.1.ssid',
  'wpasupplicant.status',
  'wpasupplicant.device.1.status',
  'wpasupplicant.profile.1.network.1.ssid',
  // Secured-AP support (verified live): station assoc to a
  // WPA2-PSK AP needs the key plus key_mgmt flipped from NONE. Values are
  // redacted in every log/audit surface; only the SOURCE is recorded.
  'wpasupplicant.profile.1.network.1.psk',
  'wpasupplicant.profile.1.network.1.key_mgmt.1.name',
] as const;
export type AllowlistedKey = (typeof ALLOWLISTED_KEYS)[number];

export interface CfgEdit {
  key: AllowlistedKey;
  value: string;
}

export function escapeSedValue(value: string): string {
  if (/["'`$\\!;|&<>]/.test(value)) throw new Error('Edit value contains shell metacharacters');
  return value.replaceAll('/', '\\/').replaceAll('\n', '');
}

export function buildSetCommand(edit: CfgEdit): string {
  if (!(ALLOWLISTED_KEYS as readonly string[]).includes(edit.key)) {
    throw new Error(`Refusing to edit non-allowlisted key: ${edit.key}`);
  }
  return `sed -i 's/^${edit.key}=.*/${edit.key}=${escapeSedValue(edit.value)}/' /tmp/system.cfg`;
}

export function buildVerifyCommand(keys: AllowlistedKey[]): string {
  return `grep -E '^(${keys.join('|')})=' /tmp/system.cfg`;
}

export const SAVE_COMMAND = '/usr/etc/rc.d/rc.softrestart save';
export const REBOOT_COMMAND = 'reboot';

export interface SshRunner {
  exec(host: string, credential: DeviceTypeCredential, command: string, timeoutMs: number): Promise<string>;
  put(host: string, credential: DeviceTypeCredential, remotePath: string, content: string, timeoutMs: number): Promise<void>;
  /** Write content to remotePath via the exec channel's stdin. airOS
   * dropbear ships NO sftp-server, so put() cannot work there — every
   * snapshot restore must use write(). Content never appears in any
   * command string (unlike heredoc/echo construction). */
  write(host: string, credential: DeviceTypeCredential, remotePath: string, content: string, timeoutMs: number): Promise<void>;
  execMany(host: string, credential: DeviceTypeCredential, commands: string[], timeoutMs: number): Promise<string[]>;
}

export class Ssh2Runner implements SshRunner {
  // Sealed for tests: inject a fake Client factory to simulate hangs without
  // a network. Production always uses the default real Client.
  constructor(
    private clientFactory: () => Client = () => new Client(),
    /** Base of the reconnect backoff (attempt i waits base*(i+1)); tests shrink it. */
    private retryBaseMs = 10000,
  ) {}

  private connectShell(host: string, credential: DeviceTypeCredential, signal?: AbortSignal): Promise<Client> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(new Error('SSH connect cancelled')); return; }
      const conn = this.clientFactory();
      let settled = false;
      const destroy = () => { try { conn.destroy(); } catch { /* already gone */ } };
      const onAbort = () => {
        if (settled) return;
        settled = true;
        destroy();
        reject(new Error('SSH connect cancelled'));
      };
      signal?.addEventListener('abort', onAbort, { once: true });
      conn.on('ready', () => {
        if (settled) { destroy(); return; } // became ready after the caller gave up: never use it
        settled = true;
        signal?.removeEventListener('abort', onAbort);
        resolve(conn);
      });
      conn.on('error', (err) => {
        if (settled) return;
        settled = true;
        signal?.removeEventListener('abort', onAbort);
        reject(err);
      });
      conn.connect({
        host, port: 22, username: credential.username, password: credential.password,
        readyTimeout: 30000,
        ...sshHostKeyOptions(host, 22),
        keepaliveInterval: 15000,
        keepaliveCountMax: 3,
        // airOS dropbear serves legacy ssh-rsa host keys; modern clients
        // refuse them by default ("no matching host key type found").
        // Verified live against an airOS AP: without this block even
        // the correct password can never be tried.
        algorithms: {
          serverHostKey: ['ssh-rsa', 'rsa-sha2-256', 'rsa-sha2-512', 'ecdsa-sha2-nistp256', 'ssh-ed25519'],
        },
      });
    });
  }

  private sleep(ms: number, signal?: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) { reject(new Error('SSH connect cancelled')); return; }
      const timer = setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve(); }, ms);
      const onAbort = () => { clearTimeout(timer); reject(new Error('SSH connect cancelled')); };
      signal?.addEventListener('abort', onAbort, { once: true });
    });
  }

  /**
   * Connect with backoff: dropbear rate-limits rapid reconnects (refused/handshake errors).
   * Cancellable: once `signal` aborts (the caller's timeout fired) no further
   * attempt is made and an in-flight handshake is destroyed.
   */
  private async connectWithRetry(host: string, credential: DeviceTypeCredential, attempts = 5, signal?: AbortSignal): Promise<Client> {
    let lastError: unknown = null;
    for (let i = 0; i < attempts; i++) {
      if (signal?.aborted) throw new Error('SSH connect cancelled');
      try {
        return await this.connectShell(host, credential, signal);
      } catch (e) {
        lastError = e;
        if (signal?.aborted || i === attempts - 1) break;
        await this.sleep(this.retryBaseMs * (i + 1), signal);
      }
    }
    throw lastError instanceof Error ? lastError : new Error('SSH connect failed after retries');
  }

  /**
   * Shared skeleton for exec/put/write.
   *
   * RACE FIX (audit): on timeout the old code destroyed `live` (still null,
   * because connectWithRetry had not returned) while connectWithRetry kept
   * retrying for up to ~150s, so a late connect executed the command AFTER the
   * caller had been told it timed out. Now the timeout aborts an
   * AbortController: retries stop, an in-flight handshake is destroyed, and a
   * connection that still arrives is destroyed before anything is executed.
   */
  private withConnection<T>(
    host: string,
    credential: DeviceTypeCredential,
    timeoutMs: number,
    timeoutMessage: string,
    body: (conn: Client, resolve: (value: T) => void, reject: (error: Error) => void) => void,
  ): Promise<T> {
    return new Promise<T>((resolveOuter, rejectOuter) => {
      const abort = new AbortController();
      let live: Client | null = null;
      let done = false;
      const finish = (conn: Client | null, endGracefully: boolean, settle: () => void) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (conn) {
          try { if (endGracefully) conn.end(); else conn.destroy(); } catch { /* already gone */ }
        }
        settle();
      };
      const timer = setTimeout(() => {
        // LEAK FIX (earlier): never reject without closing the connection, or
        // authenticated dropbear sessions pile up until MaxStartups refuses SSH.
        finish(live, false, () => rejectOuter(new Error(timeoutMessage)));
        abort.abort();
      }, timeoutMs);
      this.connectWithRetry(host, credential, 5, abort.signal).then((conn) => {
        live = conn;
        // The caller may have timed out while we were connecting: never execute then.
        if (done || abort.signal.aborted) { try { conn.destroy(); } catch { /* gone */ } return; }
        try {
          body(
            conn,
            (value) => finish(conn, true, () => resolveOuter(value)),
            (error) => finish(conn, true, () => rejectOuter(error)),
          );
        } catch (e) {
          finish(conn, false, () => rejectOuter(e instanceof Error ? e : new Error(String(e))));
        }
      }).catch((err: unknown) => {
        finish(null, false, () => rejectOuter(err instanceof Error ? err : new Error(String(err))));
      });
    });
  }

  /**
   * Run multiple commands over ONE connection. airOS dropbear throttles
   * rapid reconnects, so an apply cycle (snapshot + edits + verify) must
   * not open a connection per command. Returns outputs in order.
   */
  async execMany(host: string, credential: DeviceTypeCredential, commands: string[], timeoutMs: number): Promise<string[]> {
    const conn = await this.connectWithRetry(host, credential);
    // LEAK FIX (INC-F-01): the per-command timeout previously did
    // `live = null; reject(...)` — nulling the reference WITHOUT destroying
    // the connection, stranding an authenticated dropbear session per timeout.
    // Four INC-F-01 apply cycles piled sessions until MaxStartups refused new
    // SSH (write path starved first) while ping/HTTP stayed green. Same
    // destroy-first discipline as exec()'s earlier fix; a flag marks the
    // connection dead so the outer finally does not reuse it.
    let dead = false;
    const kill = () => {
      if (dead) return;
      dead = true;
      try { conn.destroy(); } catch { /* already gone */ }
    };
    try {
      const outputs: string[] = [];
      for (const command of commands) {
        outputs.push(await new Promise<string>((resolve, reject) => {
          const timer = setTimeout(() => { kill(); reject(new Error(`SSH command timed out after ${timeoutMs}ms`)); }, timeoutMs);
          conn.exec(command, (err, stream) => {
            if (err) { clearTimeout(timer); reject(err); return; }
            let output = '';
            stream.on('data', (d: Buffer) => { output += d.toString(); });
            stream.on('close', () => { clearTimeout(timer); resolve(output); });
            stream.on('error', (e: Error) => { clearTimeout(timer); reject(e); });
          });
        }));
      }
      return outputs;
    } finally {
      // Dead connections were already destroyed exactly once by kill();
      // only gracefully end a connection that never timed out.
      try { if (dead) kill(); else conn.end(); } catch { /* best effort */ }
    }
  }

  exec(host: string, credential: DeviceTypeCredential, command: string, timeoutMs: number): Promise<string> {
    return this.withConnection<string>(host, credential, timeoutMs, `SSH timed out after ${timeoutMs}ms`, (conn, resolve, reject) => {
      conn.exec(command, (err, stream) => {
        if (err) { reject(err); return; }
        let output = '';
        stream.on('data', (d: Buffer) => { output += d.toString(); });
        stream.on('close', () => resolve(output));
        stream.on('error', (e: Error) => reject(e));
      });
    });
  }

  put(host: string, credential: DeviceTypeCredential, remotePath: string, content: string, timeoutMs: number): Promise<void> {
    return this.withConnection<void>(host, credential, timeoutMs, `SFTP timed out after ${timeoutMs}ms`, (conn, resolve, reject) => {
      conn.sftp((err, sftp) => {
        if (err) { reject(err); return; }
        const stream = sftp.createWriteStream(remotePath);
        stream.on('close', () => resolve());
        stream.on('error', (e: Error) => reject(e));
        stream.end(content);
      });
    });
  }

  write(host: string, credential: DeviceTypeCredential, remotePath: string, content: string, timeoutMs: number): Promise<void> {
    // The remote command is a fixed string; content travels on stdin, so
    // key material never appears in command strings, ps output, or logs.
    const safePath = remotePath.replace(/[^A-Za-z0-9_./-]/g, '');
    if (!safePath || safePath !== remotePath) return Promise.reject(new Error('Refusing unsafe remote path'));
    return this.withConnection<void>(host, credential, timeoutMs, `Write timed out after ${timeoutMs}ms`, (conn, resolve, reject) => {
      conn.exec(`cat > ${safePath}`, (err, stream) => {
        if (err) { reject(err); return; }
        let stderr = '';
        stream.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });
        stream.on('close', (code: number) => {
          if (code !== 0) reject(new Error(`Remote write failed (exit ${code}): ${stderr.slice(0, 200)}`));
          else resolve();
        });
        stream.on('error', (e: Error) => reject(e));
        stream.write(content);
        stream.end();
      });
    });
  }
}

export interface ApplyResult {
  applied: boolean;
  snapshotBytes: number;
  verifiedKeys: string[];
  rolledBack: boolean;
  detail: string;
}

/** Path of the boot-id record for a host (inside the private snapshot directory). */
export function bootIdFile(host: string): string {
  return join(snapshotDir(), bootIdName(host));
}
/** Parse `key=value` lines from a cfg dump into a map. */
export function parseCfg(text: string): Map<string, string> {
  const map = new Map<string, string>();
  for (const line of text.split('\n')) {
    const eq = line.indexOf('=');
    if (eq > 0 && !line.startsWith('#')) map.set(line.slice(0, eq), line.slice(eq + 1));
  }
  return map;
}

/**
 * Full apply cycle: snapshot -> sed edits -> re-read verify -> softrestart
 * save -> caller-supplied post verification -> auto-rollback+restore on miss.
 * verifyAfterSave is injected so tests never touch the network; production
 * passes a readback (e.g. allowlisted status fetch) for the confirm deadline.
 *
 * NO full reboot after save by default (INC-C-01, verified live:
 * association forms on the softrestart save alone; the reboot was the crash
 * vector — and INC-F-01 proved the self-inflicted dark window
 * breaks the cycle itself: post-save verify fails against the rebooting
 * radio, then the rollback write times out against down dropbear). Pass
 * { rebootAfterSave: true } only when a specific link demonstrably needs it.
 */
export async function applyCfgEdits(
  runner: SshRunner,
  host: string,
  credential: DeviceTypeCredential,
  edits: CfgEdit[],
  verifyAfterSave: () => Promise<{ ok: boolean; detail: string }>,
  timeoutMs = 30000,
  options?: { rebootAfterSave?: boolean },
): Promise<ApplyResult> {
  // Reboot tripwire (field lesson): a target that rebooted since our
  // last operation must NEVER be written to blindly. /proc boot_id changes
  // on every boot; compare against the recorded value before proceeding.
  const bootNow = (await runner.exec(host, credential, 'cat /proc/sys/kernel/random/boot_id', timeoutMs)).trim();
  if (!/^[0-9a-f-]{36}$/.test(bootNow)) throw new Error('Could not read target boot_id; refusing (unknown reboot state)');
  try {
    const prior = (readSnapshotFile(bootIdName(host)) ?? '').trim();
    if (prior && prior !== bootNow) {
      throw new Error(`Target rebooted since last operation (boot_id changed); refusing write. Re-verify full state before retrying.`);
    }
  } catch (e) {
    if (e instanceof Error && (e.message.startsWith('Target rebooted') || e.message.startsWith('Snapshot directory'))) throw e;
    // No prior record: first contact. Record and continue.
  }
  // One connection for snapshot + all edits + re-read: dropbear throttles
  // rapid reconnects, so these must never be separate sessions.
  const batch = await runner.execMany(host, credential,
    ['cat /tmp/system.cfg', ...edits.map((e) => buildSetCommand(e)), buildVerifyCommand(edits.map((e) => e.key))],
    timeoutMs);
  const snapshot = batch[0];
  if (!snapshot.includes('wireless.1.ssid=')) throw new Error('Snapshot does not look like an airOS system.cfg; refusing');
  const recheck = parseCfg(batch[batch.length - 1]);
  const bad = edits.filter((e) => recheck.get(e.key) !== e.value).map((e) => e.key);
  if (bad.length) {
    await restoreSnapshot(runner, host, credential, snapshot, timeoutMs);
    return { applied: false, snapshotBytes: snapshot.length, verifiedKeys: [], rolledBack: true, detail: `Re-read mismatch on ${bad.join(',')}; change never saved, snapshot restored` };
  }
  await runner.exec(host, credential, SAVE_COMMAND, timeoutMs);
  if (options?.rebootAfterSave) {
    await runner.exec(host, credential, REBOOT_COMMAND, Math.min(timeoutMs, 15000)).catch(() => { /* reboot drops SSH by design */ });
  }
  const check = await verifyAfterSave();
  if (!check.ok) {
    await restoreSnapshot(runner, host, credential, snapshot, timeoutMs);
    return { applied: false, snapshotBytes: snapshot.length, verifiedKeys: edits.map((e) => e.key), rolledBack: true, detail: `Post-save verification failed (${check.detail}); snapshot restored (safe-mode rollback)` };
  }
  try {
    writeSnapshotFile(bootIdName(host), bootNow + '\n');
  } catch { /* boot tracking is best-effort; the apply itself verified */ }
  return { applied: true, snapshotBytes: snapshot.length, verifiedKeys: edits.map((e) => e.key), rolledBack: false, detail: check.detail };
}

export async function restoreSnapshot(
  runner: SshRunner,
  host: string,
  credential: DeviceTypeCredential,
  snapshot: string,
  timeoutMs = 30000,
  options?: { rebootAfterSave?: boolean },
): Promise<void> {
  // stdin-stream write: airOS dropbear has no sftp-server (verified live
  //  — SFTP put fails with "Connection closed" there).
  await runner.write(host, credential, '/tmp/system.cfg', snapshot, timeoutMs);
  await runner.exec(host, credential, SAVE_COMMAND, timeoutMs);
  // Same no-reboot default as applyCfgEdits (softrestart save activates;
  // a reboot here reintroduces the INC-F-01 dark-window failure).
  if (options?.rebootAfterSave) {
    await runner.exec(host, credential, REBOOT_COMMAND, Math.min(timeoutMs, 15000)).catch(() => { /* reboot drops SSH by design */ });
  }
}
