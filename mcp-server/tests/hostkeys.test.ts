import { describe, it, expect, afterEach, beforeEach } from 'vitest';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Client, Server, utils } from 'ssh2';
import {
  checkFingerprint, hexToSshFingerprint, hostKeyMode, setHostKeyEventSink, sshHostKeyOptions, tlsFingerprint, type HostKeyEvent,
} from '../src/security/hostkeys.js';

let dir: string;
let events: HostKeyEvent[];
const envFor = (mode: string): NodeJS.ProcessEnv => ({ NOC_SSH_HOSTKEY_MODE: mode, NOC_KNOWN_HOSTS_FILE: join(dir, 'known_hosts.json') });
const FP1 = 'SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
const FP2 = 'SHA256:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB';

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'noc-hk-'));
  events = [];
  setHostKeyEventSink((e) => events.push(e));
});
afterEach(() => {
  setHostKeyEventSink(null);
  rmSync(dir, { recursive: true, force: true });
});

describe('host key mode parsing', () => {
  it('defaults to warn; accepts the four modes; a typo fails closed to strict', () => {
    expect(hostKeyMode({})).toBe('warn');
    expect(hostKeyMode({ NOC_SSH_HOSTKEY_MODE: '' })).toBe('warn');
    for (const m of ['off', 'warn', 'tofu', 'strict']) expect(hostKeyMode({ NOC_SSH_HOSTKEY_MODE: m.toUpperCase() })).toBe(m);
    expect(hostKeyMode({ NOC_SSH_HOSTKEY_MODE: 'stirct' })).toBe('strict');
  });
});

describe('fingerprint store', () => {
  it('warn: records NEW keys, audits CHANGED keys, never blocks, stores 0600 atomically', () => {
    const env = envFor('warn');
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, env)).toEqual({ allow: true, event: 'new' });
    expect(events.map((e) => e.event)).toEqual(['new']);
    const file = join(dir, 'known_hosts.json');
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(readdirSync(dir)).toEqual(['known_hosts.json']); // no leftover temp files
    // same key again: silent
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, env)).toEqual({ allow: true });
    expect(events).toHaveLength(1);
    // a factory-reset radio rotates its key: allowed, audited, history kept
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP2, env)).toEqual({ allow: true, event: 'changed' });
    expect(events[1]).toMatchObject({ event: 'changed', previous: FP1, fingerprint: FP2, blocked: false });
    const stored = JSON.parse(readFileSync(file, 'utf8')) as { hosts: Record<string, { fingerprint: string; history: string[] }> };
    expect(stored.hosts['ssh:10.0.0.1:22'].fingerprint).toBe(FP2);
    expect(stored.hosts['ssh:10.0.0.1:22'].history).toEqual([FP1]);
  });

  it('tracks host and port separately', () => {
    const env = envFor('warn');
    checkFingerprint('ssh', '10.0.0.1', 22, FP1, env);
    expect(checkFingerprint('ssh', '10.0.0.1', 2222, FP2, env).event).toBe('new');
    expect(checkFingerprint('ssh', '10.0.0.2', 22, FP2, env).event).toBe('new');
  });

  it('tofu: records unknown keys, BLOCKS a changed key and keeps the pinned one', () => {
    const env = envFor('tofu');
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, env).allow).toBe(true);
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP2, env)).toEqual({ allow: false, event: 'blocked' });
    expect(events.at(-1)).toMatchObject({ event: 'blocked', blocked: true, previous: FP1 });
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, env).allow).toBe(true); // original key still trusted
  });

  it('strict: blocks unknown and changed keys', () => {
    const env = envFor('strict');
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, env)).toEqual({ allow: false, event: 'blocked' });
    expect(existsSync(join(dir, 'known_hosts.json'))).toBe(false);
    writeFileSync(join(dir, 'known_hosts.json'), JSON.stringify({ version: 1, hosts: { 'ssh:10.0.0.1:22': { fingerprint: FP1, firstSeen: 'x', lastSeen: 'x', history: [] } } }));
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, env).allow).toBe(true);
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP2, env).allow).toBe(false);
  });

  it('off: no verification, no file, no ssh2 options', () => {
    const env = envFor('off');
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, env)).toEqual({ allow: true });
    expect(existsSync(join(dir, 'known_hosts.json'))).toBe(false);
    const prior = process.env.NOC_SSH_HOSTKEY_MODE;
    process.env.NOC_SSH_HOSTKEY_MODE = 'off';
    try { expect(sshHostKeyOptions('10.0.0.1', 22)).toEqual({}); } finally {
      if (prior === undefined) delete process.env.NOC_SSH_HOSTKEY_MODE; else process.env.NOC_SSH_HOSTKEY_MODE = prior;
    }
  });

  it('a corrupt store never gets overwritten: warn continues, tofu/strict fail closed', () => {
    const file = join(dir, 'known_hosts.json');
    writeFileSync(file, '{not json');
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, envFor('warn')).allow).toBe(true);
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, envFor('tofu')).allow).toBe(false);
    expect(checkFingerprint('ssh', '10.0.0.1', 22, FP1, envFor('strict')).allow).toBe(false);
    expect(readFileSync(file, 'utf8')).toBe('{not json');
    expect(events.every((e) => e.event === 'store_error')).toBe(true);
  });

  it('TLS certificates are observed only, never blocked, even in strict mode', () => {
    const env = envFor('strict');
    expect(checkFingerprint('tls', '10.0.0.5', 443, FP1, env).allow).toBe(true);
    expect(checkFingerprint('tls', '10.0.0.5', 443, FP2, env).allow).toBe(true);
    expect(events.map((e) => e.event)).toEqual(['new', 'changed']);
  });

  it('fingerprint formatting matches OpenSSH (SHA256:<unpadded base64>)', () => {
    expect(hexToSshFingerprint('00'.repeat(32))).toBe(`SHA256:${'A'.repeat(43)}`);
    expect(tlsFingerprint(Array.from({ length: 32 }, () => '00').join(':'))).toBe(`SHA256:${'A'.repeat(43)}`);
  });
});

describe('end to end against a real ssh2 server', () => {
  async function withServer(): Promise<{ port: number; close: () => void }> {
    const pair = utils.generateKeyPairSync('ed25519');
    const server = new Server({ hostKeys: [pair.private] }, (client) => {
      client.on('authentication', (ctx) => ctx.accept());
      client.on('error', () => undefined);
      client.on('ready', () => undefined);
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    const address = server.address();
    const port = typeof address === 'object' && address ? address.port : 0;
    return { port, close: () => server.close() };
  }
  function connect(port: number): Promise<'ready' | string> {
    return new Promise((resolve) => {
      const c = new Client();
      c.on('ready', () => { c.end(); resolve('ready'); });
      c.on('error', (e) => resolve(e.message));
      c.connect({ host: '127.0.0.1', port, username: 'u', password: 'p', readyTimeout: 5000, ...sshHostKeyOptions('127.0.0.1', port) });
    });
  }

  it('warn mode connects, records the real server key, and pins it for strict mode', async () => {
    const srv = await withServer();
    const prior = { mode: process.env.NOC_SSH_HOSTKEY_MODE, file: process.env.NOC_KNOWN_HOSTS_FILE };
    process.env.NOC_KNOWN_HOSTS_FILE = join(dir, 'known_hosts.json');
    try {
      process.env.NOC_SSH_HOSTKEY_MODE = 'warn';
      expect(await connect(srv.port)).toBe('ready');
      expect(events.map((e) => e.event)).toEqual(['new']);
      expect(events[0].fingerprint).toMatch(/^SHA256:[A-Za-z0-9+/]{43}$/);
      process.env.NOC_SSH_HOSTKEY_MODE = 'strict';
      expect(await connect(srv.port)).toBe('ready'); // same key, pinned
      // a different server on the same host:port key slot = changed key => blocked in strict
      const stored = JSON.parse(readFileSync(join(dir, 'known_hosts.json'), 'utf8')) as { hosts: Record<string, { fingerprint: string }> };
      stored.hosts[`ssh:127.0.0.1:${srv.port}`].fingerprint = FP2;
      writeFileSync(join(dir, 'known_hosts.json'), JSON.stringify(stored));
      expect(await connect(srv.port)).toMatch(/denied|verif/i);
      expect(events.at(-1)).toMatchObject({ event: 'blocked', blocked: true });
    } finally {
      srv.close();
      for (const [k, v] of [['NOC_SSH_HOSTKEY_MODE', prior.mode], ['NOC_KNOWN_HOSTS_FILE', prior.file]] as const) {
        if (v === undefined) delete process.env[k]; else process.env[k] = v;
      }
    }
  });
});
