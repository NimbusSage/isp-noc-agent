import { describe, it, expect } from 'vitest';
import { EventEmitter } from 'node:events';
import type { Client } from 'ssh2';
import { Ssh2Runner } from '../src/temp-site/airos-ssh.js';
import type { DeviceTypeCredential } from '../src/credentials/schema.js';

const CRED = { name: 't', username: 'ubnt', password: 'x', priority: 0, tags: [] } as DeviceTypeCredential;
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

interface FakeConn extends EventEmitter {
  destroyed: number;
  ended: number;
  execCalls: string[];
  shellCalls: string[];
  connectCalls: number;
}

/** A scripted fake ssh2 Client: `behavior` decides what connect() does. */
function makeFactory(behavior: (conn: FakeConn, attempt: number) => void) {
  const conns: FakeConn[] = [];
  const factory = (): Client => {
    const conn = new EventEmitter() as FakeConn;
    conn.destroyed = 0; conn.ended = 0; conn.execCalls = []; conn.shellCalls = []; conn.connectCalls = 0;
    const attempt = conns.length;
    conns.push(conn);
    const anyConn = conn as unknown as Record<string, unknown>;
    anyConn.connect = () => { conn.connectCalls++; behavior(conn, attempt); };
    anyConn.destroy = () => { conn.destroyed++; return true; };
    anyConn.end = () => { conn.ended++; return true; };
    anyConn.exec = (cmd: string, cb: (e: Error | null, s?: EventEmitter) => void) => {
      conn.execCalls.push(cmd);
      const stream = new EventEmitter();
      (stream as unknown as Record<string, unknown>).stderr = new EventEmitter();
      (stream as unknown as Record<string, unknown>).write = () => true;
      (stream as unknown as Record<string, unknown>).end = () => undefined;
      cb(null, stream);
      setImmediate(() => { stream.emit('data', Buffer.from('ran')); stream.emit('close', 0); });
    };
    // SHELL-channel stub: Ssh2Runner.write() now writes via an interactive
    // shell (airOS dropbear stalls on exec-channel stdin), so the fake must
    // model conn.shell too. Shell calls are recorded separately.
    anyConn.shellCalls = [];
    anyConn.shell = (cb: (e: Error | null, s?: EventEmitter) => void) => {
      conn.shellCalls.push('shell');
      const stream = new EventEmitter();
      (stream as unknown as Record<string, unknown>).stderr = new EventEmitter();
      (stream as unknown as Record<string, unknown>).write = () => true;
      (stream as unknown as Record<string, unknown>).end = () => undefined;
      cb(null, stream);
    };
    return conn as unknown as Client;
  };
  return { factory, conns };
}

describe('Ssh2Runner timeout race (late connect must never execute)', () => {
  it('destroys a connection that becomes ready AFTER the caller timed out, and never execs', async () => {
    const { factory, conns } = makeFactory((conn) => { setTimeout(() => conn.emit('ready'), 250); });
    const runner = new Ssh2Runner(factory, 10);
    await expect(runner.exec('10.0.0.1', CRED, 'reboot', 60)).rejects.toThrow(/timed out/);
    await wait(400);
    expect(conns).toHaveLength(1);
    expect(conns[0].execCalls).toEqual([]);
    expect(conns[0].destroyed).toBeGreaterThanOrEqual(1);
  });

  it('stops retrying once the caller timed out (no second attempt during backoff)', async () => {
    const { factory, conns } = makeFactory((conn) => { setImmediate(() => conn.emit('error', new Error('connect ECONNREFUSED'))); });
    const runner = new Ssh2Runner(factory, 200); // 200ms backoff before attempt 2
    await expect(runner.exec('10.0.0.1', CRED, 'uptime', 80)).rejects.toThrow(/timed out/);
    await wait(600);
    expect(conns).toHaveLength(1);
  });

  it('applies the same protection to write() and put()', async () => {
    const { factory, conns } = makeFactory((conn) => { setTimeout(() => conn.emit('ready'), 200); });
    const runner = new Ssh2Runner(factory, 10);
    // Newline-terminated: write() refuses non-newline content before any
    // connection (EOT-EOF guard), which would mask the race being tested.
    await expect(runner.write('10.0.0.1', CRED, '/tmp/system.cfg', 'x\n', 50)).rejects.toThrow(/timed out/);
    await expect(runner.put('10.0.0.1', CRED, '/tmp/system.cfg', 'x', 50)).rejects.toThrow(/timed out/);
    await wait(350);
    for (const c of conns) { expect(c.execCalls).toEqual([]); expect(c.destroyed).toBeGreaterThanOrEqual(1); }
    // The late connection was destroyed before any shell channel opened.
    for (const c of conns) { expect(c.shellCalls).toEqual([]); }
  });

  it('destroys an in-flight handshake when the timeout fires', async () => {
    const { factory, conns } = makeFactory(() => { /* never ready, never errors */ });
    const runner = new Ssh2Runner(factory, 10);
    await expect(runner.exec('10.0.0.1', CRED, 'uptime', 50)).rejects.toThrow(/timed out/);
    expect(conns[0].destroyed).toBeGreaterThanOrEqual(1);
  });

  it('still runs a command normally and ends the connection gracefully', async () => {
    const { factory, conns } = makeFactory((conn) => { setImmediate(() => conn.emit('ready')); });
    const runner = new Ssh2Runner(factory, 10);
    await expect(runner.exec('10.0.0.1', CRED, 'uptime', 2000)).resolves.toBe('ran');
    expect(conns[0].execCalls).toEqual(['uptime']);
    expect(conns[0].ended).toBe(1);
    expect(conns[0].destroyed).toBe(0);
  });

  it('retries a transient connect failure when the caller is still waiting', async () => {
    const { factory, conns } = makeFactory((conn, attempt) => {
      setImmediate(() => (attempt === 0 ? conn.emit('error', new Error('connect ECONNRESET')) : conn.emit('ready')));
    });
    const runner = new Ssh2Runner(factory, 5);
    await expect(runner.exec('10.0.0.1', CRED, 'uptime', 3000)).resolves.toBe('ran');
    expect(conns).toHaveLength(2);
    expect(conns[0].execCalls).toEqual([]);
    expect(conns[1].execCalls).toEqual(['uptime']);
  });

  it('refuses an unsafe remote path without connecting', async () => {
    const { factory, conns } = makeFactory(() => undefined);
    const runner = new Ssh2Runner(factory, 5);
    await expect(runner.write('10.0.0.1', CRED, '/tmp/x; reboot', 'c', 1000)).rejects.toThrow(/unsafe remote path/);
    expect(conns).toHaveLength(0);
  });
});
