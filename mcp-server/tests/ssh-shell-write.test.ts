import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EventEmitter } from 'node:events';
import type { Client } from 'ssh2';
import { Ssh2Runner } from '../src/temp-site/airos-ssh.js';
import type { DeviceTypeCredential } from '../src/credentials/schema.js';

const CRED = { name: 't', username: 'ubnt', password: 'x', priority: 0, tags: [] } as DeviceTypeCredential;
const PATH = '/tmp/system.cfg';
const EOT = '\u0004';
const realTick = () => new Promise<void>((r) => setImmediate(r));

interface Shell { writes: string[]; ended: number; stream: EventEmitter }
interface Rig { factory: () => Client; shells: Shell[]; conn: () => EventEmitter & { connectCalls: number; destroyed: number; ended: number; execCalls: string[] } }

/**
 * A fake airOS dropbear: it accepts a shell, records what is typed, and answers the byte-count probe the way the
 * device does. `answer(received)` returns the text the device prints for the probe (default: the exact byte count).
 */
function rig(answer: (receivedBytes: number) => string | null = (n) => `__NOCW__\r\n${n}\r\n__NOCW__\r\n`): Rig {
  const shells: Shell[] = [];
  let made: (EventEmitter & { connectCalls: number; destroyed: number; ended: number; execCalls: string[] }) | undefined;
  const factory = (): Client => {
    const conn = new EventEmitter() as EventEmitter & { connectCalls: number; destroyed: number; ended: number; execCalls: string[] };
    conn.connectCalls = 0; conn.destroyed = 0; conn.ended = 0; conn.execCalls = [];
    made = conn;
    const any = conn as unknown as Record<string, unknown>;
    any.connect = () => { conn.connectCalls++; setImmediate(() => conn.emit('ready')); };
    any.destroy = () => { conn.destroyed++; return true; };
    any.end = () => { conn.ended++; return true; };
    any.exec = (cmd: string, cb: (e: Error | null, s?: EventEmitter) => void) => {
      conn.execCalls.push(cmd);
      const s = new EventEmitter();
      (s as unknown as Record<string, unknown>).stderr = new EventEmitter();
      cb(null, s);
    };
    any.shell = (cb: (e: Error | null, s?: EventEmitter) => void) => {
      const stream = new EventEmitter();
      const shell: Shell = { writes: [], ended: 0, stream };
      shells.push(shell);
      (stream as unknown as Record<string, unknown>).stderr = new EventEmitter();
      (stream as unknown as Record<string, unknown>).end = () => { shell.ended++; };
      (stream as unknown as Record<string, unknown>).write = (data: string) => {
        shell.writes.push(data);
        if (data.includes('wc -c')) {
          // bytes typed between the `cat > path` line and the EOT
          const body = shell.writes.slice(1, shell.writes.indexOf(EOT));
          const reply = answer(Buffer.byteLength(body.join(''), 'utf8'));
          if (reply !== null) setImmediate(() => stream.emit('data', Buffer.from(reply)));
        }
        return true;
      };
      cb(null, stream);
    };
    return conn as unknown as Client;
  };
  return { factory, shells, conn: () => made! };
}

async function run(r: Rig, content: string, timeoutMs: number, advanceMs: number, path = PATH): Promise<{ ok: boolean; error?: Error }> {
  const runner = new Ssh2Runner(r.factory, 10);
  let outcome: { ok: boolean; error?: Error } | undefined;
  const p = runner.write('192.0.2.10', CRED, path, content, timeoutMs).then(() => { outcome = { ok: true }; }, (e: Error) => { outcome = { ok: false, error: e }; });
  await realTick(); await realTick();                 // connect, open the shell, schedule the typing
  await vi.advanceTimersByTimeAsync(advanceMs);
  await realTick();
  await vi.advanceTimersByTimeAsync(0);
  await p;
  return outcome!;
}

describe('Ssh2Runner.write over a shell channel', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }); });
  afterEach(() => { vi.useRealTimers(); });

  it('types cat, the content, EOT, then the probe, and resolves on the exact byte count', async () => {
    const r = rig();
    const content = 'radio.1.mode=managed\nwireless.1.ssid=TEST\n';
    const out = await run(r, content, 10_000, 3_000);
    expect(out).toEqual({ ok: true });
    const w = r.shells[0].writes;
    expect(w[0]).toBe(`cat > ${PATH}\n`);
    expect(w[1]).toBe(content);
    expect(w[2]).toBe(EOT);
    expect(w[3]).toBe(`echo __NOCW__; wc -c < ${PATH}; echo __NOCW__\n`);
    expect(r.shells[0].ended).toBe(1);
    expect(r.conn().execCalls).toEqual([]);            // the exec channel is never used for the payload
    expect(r.conn().ended).toBeGreaterThanOrEqual(1);
  });

  it('counts bytes, not characters', async () => {
    const r = rig();
    const out = await run(r, 'ssid=café\n', 10_000, 3_000);
    expect(out.ok).toBe(true);
  });

  it('accepts a count padded with blanks', async () => {
    const r = rig((n) => `__NOCW__\r\n   ${n}  \r\n__NOCW__\r\n`);
    expect((await run(r, 'a=1\n', 10_000, 3_000)).ok).toBe(true);
  });

  it('does not report success when the device saw fewer bytes than were sent', async () => {
    const r = rig((n) => `__NOCW__\r\n${n - 3}\r\n__NOCW__\r\n`);
    const out = await run(r, 'a=1\nb=2\n', 4_000, 4_500);
    expect(out.ok).toBe(false);
    expect(out.error?.message).toMatch(/Write timed out/);
    expect(r.conn().destroyed).toBeGreaterThanOrEqual(1);
  });

  it('does not take the echo of the typed probe for a confirmation', async () => {
    const r = rig(() => 'echo __NOCW__; wc -c < /tmp/system.cfg; echo __NOCW__\r\n');
    const out = await run(r, 'a=1\n', 4_000, 4_500);
    expect(out.ok).toBe(false);
  });

  it('fails when the shell closes before the count arrives', async () => {
    const r = rig(() => null);
    const runner = new Ssh2Runner(r.factory, 10);
    const p = runner.write('192.0.2.10', CRED, PATH, 'a=1\n', 10_000).then(() => 'resolved', (e: Error) => e.message);
    await realTick(); await realTick();
    r.shells[0].stream.emit('close');
    expect(await p).toMatch(/closed before byte-count confirmation/);
  });

  it('refuses content that does not end in a newline, before connecting', async () => {
    const r = rig();
    const runner = new Ssh2Runner(r.factory, 10);
    await expect(runner.write('192.0.2.10', CRED, PATH, 'no newline', 1_000)).rejects.toThrow(/does not end in a newline/);
    expect(r.shells).toHaveLength(0);
  });

  it('refuses an unsafe remote path, before connecting', async () => {
    const r = rig();
    const runner = new Ssh2Runner(r.factory, 10);
    for (const bad of ['/tmp/a b', '/tmp/x;reboot', '/tmp/$(id)']) {
      await expect(runner.write('192.0.2.10', CRED, bad, 'a=1\n', 1_000)).rejects.toThrow(/unsafe remote path/);
    }
    expect(r.shells).toHaveLength(0);
  });
});
