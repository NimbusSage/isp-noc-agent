import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';
import {
  buildSetCommand,
  buildVerifyCommand,
  escapeSedValue,
  parseCfg,
  applyCfgEdits,
  SAVE_COMMAND,
  type SshRunner,
} from '../src/temp-site/airos-ssh.js';
import type { DeviceTypeCredential } from '../src/credentials/schema.js';

// Boot-id tripwire records are written to the private snapshot directory;
// keep every test run out of the install's real state/ directory.
const SNAP_DIR = mkdtempSync(join(tmpdir(), 'noc-airos-test-'));
const PRIOR_SNAP_DIR = process.env.TEMP_SITE_SNAPSHOT_DIR;
beforeAll(() => { process.env.TEMP_SITE_SNAPSHOT_DIR = SNAP_DIR; });
afterAll(() => {
  if (PRIOR_SNAP_DIR === undefined) delete process.env.TEMP_SITE_SNAPSHOT_DIR;
  else process.env.TEMP_SITE_SNAPSHOT_DIR = PRIOR_SNAP_DIR;
  rmSync(SNAP_DIR, { recursive: true, force: true });
});

const CRED = { name: 't', username: 'ubnt', password: 'x', priority: 0, tags: [] } as DeviceTypeCredential;

function fakeRunner(state: { cfg: string; failVerify?: boolean; bootId?: string; commands: string[] }): SshRunner {
  const runOne = async (command: string): Promise<string> => {
    state.commands.push(command);
    if (command === 'cat /tmp/system.cfg') return state.cfg;
    if (command === 'cat /proc/sys/kernel/random/boot_id') return state.bootId || '11111111-2222-3333-4444-555555555555';
      if (command.startsWith('sed -i')) {
        const keyMatch = command.match(/\^([A-Za-z0-9._]+)=\.\*\//);
        if (!keyMatch || !command.endsWith(`/' /tmp/system.cfg`)) throw new Error('bad sed in test double');
        const rest = command.slice(command.indexOf(keyMatch[0]) + keyMatch[0].length, -`/' /tmp/system.cfg`.length);
        const valMatch = rest.match(new RegExp('^' + keyMatch[1].replaceAll('.', '\\.') + '=(.*)$'));
        if (!valMatch) throw new Error('bad sed value in test double');
        const lines = state.cfg.split('\n').map((l) => (l.startsWith(keyMatch[1] + '=') ? `${keyMatch[1]}=${valMatch[1].replaceAll('\\/', '/')}` : l));
        state.cfg = lines.join('\n');
        return '';
      }
      if (command.startsWith('grep -E')) {
        if (state.failVerify) return 'radio.1.mode=master';
        const keys = command.match(/\^\(.+\)=/)?.[0].slice(2, -2).split('|') ?? [];
        return state.cfg.split('\n').filter((l) => keys.some((k) => l.startsWith(k + '='))).join('\n');
      }
      if (command === SAVE_COMMAND || command === 'reboot') return '';
      throw new Error(`unexpected command: ${command}`);
    };
  return {
    exec: async (_h, _c, command: string) => runOne(command),
    execMany: async (_h, _c, commands: string[]) => {
      const out: string[] = [];
      for (const c of commands) out.push(await runOne(c));
      return out;
    },
    put: async (_h, _c, _p: string, content: string) => { state.cfg = content; },
    // stdin-stream write: records the fixed remote command (never content).
    write: async (_h, _c, remotePath: string, content: string) => {
      if (!/^cat > \/tmp\/[A-Za-z0-9_.~-]+$/.test(`cat > ${remotePath}`)) throw new Error('unsafe write path in test double');
      state.cfg = content;
    },
  };
}

describe('airos-ssh transport', () => {
  it('builds anchored sed for allowlisted keys and rejects the rest', () => {
    expect(buildSetCommand({ key: 'radio.1.mode', value: 'managed' }))
      .toBe(`sed -i 's/^radio.1.mode=.*/radio.1.mode=managed/' /tmp/system.cfg`);
    expect(() => buildSetCommand({ key: 'users.1.password' as never, value: 'x' })).toThrow(/non-allowlisted/);
    expect(() => escapeSedValue('a;b')).toThrow(/metacharacters/);
    expect(buildVerifyCommand(['radio.1.mode', 'radio.1.freq']))
      .toBe(`grep -E '^(radio.1.mode|radio.1.freq)=' /tmp/system.cfg`);
  });

  it('applies, verifies, saves, and confirms without rollback', async () => {
    const state = { cfg: 'wireless.1.ssid=test\nradio.1.mode=master\nradio.1.freq=0\n', commands: [] as string[] };
    const r = await applyCfgEdits(fakeRunner(state), '10.0.0.1', CRED,
      [{ key: 'radio.1.mode', value: 'managed' }],
      async () => ({ ok: true, detail: 'associated' }));
    expect(r.applied).toBe(true);
    expect(r.rolledBack).toBe(false);
    expect(state.cfg).toContain('radio.1.mode=managed');
    expect(state.commands).toContain(SAVE_COMMAND);
    // Save-only default (INC-C-01/INC-F-01): no full reboot after save.
    expect(state.commands).not.toContain('reboot');
  });

  it('sends reboot after save only when explicitly requested', async () => {
    const state = { cfg: 'wireless.1.ssid=test\nradio.1.mode=master\n', commands: [] as string[] };
    const r = await applyCfgEdits(fakeRunner(state), '10.0.0.1', CRED,
      [{ key: 'radio.1.mode', value: 'managed' }],
      async () => ({ ok: true, detail: 'associated' }),
      30000, { rebootAfterSave: true });
    expect(r.applied).toBe(true);
    expect(state.commands).toContain('reboot');
  });

  it('rolls back without saving when re-read mismatches', async t => {
    const original = 'wireless.1.ssid=test\nradio.1.mode=master\n';
    const state = { cfg: original, commands: [] as string[], failVerify: true };
    const r = await applyCfgEdits(fakeRunner(state), '10.0.0.1', CRED,
      [{ key: 'radio.1.mode', value: 'managed' }],
      async () => ({ ok: true, detail: 'x' }));
    expect(r.applied).toBe(false);
    expect(r.rolledBack).toBe(true);
    expect(state.cfg).toBe(original);
    // Restore itself re-saves the snapshot; what must NOT appear is a save
    // of the changed config — verified by content equality above. Neither
    // apply nor restore reboots by default (save-only).
  });

  it('restores snapshot when post-save verification fails (safe-mode)', async () => {
    const original = 'wireless.1.ssid=test\nradio.1.mode=master\n';
    const state = { cfg: original, commands: [] as string[] };
    const r = await applyCfgEdits(fakeRunner(state), '10.0.0.1', CRED,
      [{ key: 'radio.1.mode', value: 'managed' }],
      async () => ({ ok: false, detail: 'handshake timeout' }));
    expect(r.applied).toBe(false);
    expect(r.rolledBack).toBe(true);
    expect(state.cfg).toBe(original);
  });

  it('destroys the connection on execMany timeout instead of leaking it', async () => {
    // Regression for the INC-F-01 dropbear pileup: execMany's timeout nulled
    // the reference without destroying, stranding one authenticated session
    // per timed-out command until MaxStartups refused new SSH. The fake
    // emits 'ready' immediately but never answers exec: the timeout must
    // destroy the connection.
    const { Ssh2Runner } = await import('../src/temp-site/airos-ssh.js');
    const { EventEmitter } = await import('node:events');
    let destroyed = 0;
    let ended = 0;
    const fakeConn = new EventEmitter() as unknown as import('ssh2').Client;
    (fakeConn as unknown as { destroy: () => boolean }).destroy = () => { destroyed++; return true; };
    (fakeConn as unknown as { end: () => boolean }).end = () => { ended++; return true; };
    (fakeConn as unknown as { connect: (cfg: unknown) => void }).connect = () => {
      setImmediate(() => (fakeConn as unknown as EventEmitter).emit('ready'));
    };
    (fakeConn as unknown as { exec: (cmd: string, cb: (e: Error | null) => void) => void }).exec = () => {
      // Never calls back: forces the per-command timeout.
    };
    const runner = new Ssh2Runner(() => fakeConn);
    await assert.rejects(
      () => runner.execMany('10.0.0.1', CRED, ['echo hi'], 100),
      /timed out/,
    );
    assert.equal(destroyed, 1);
    assert.equal(ended, 0); // dead connection is destroyed, not gracefully ended
  });

  it('parses cfg snapshots for verification', () => {
    expect(parseCfg('a=1\n# c\nb=2').get('b')).toBe('2');
  });

  it('refuses the write when the target rebooted since last operation', async () => {
    const { mkdtempSync, rmSync, writeFileSync } = await import('node:fs');
    const { tmpdir } = await import('node:os');
    const { join } = await import('node:path');
    const dir = mkdtempSync(join(tmpdir(), 'bootid-'));
    const prior = process.env.TEMP_SITE_SNAPSHOT_DIR;
    process.env.TEMP_SITE_SNAPSHOT_DIR = dir;
    try {
      const state = { cfg: 'wireless.1.ssid=test\nradio.1.mode=master\n', commands: [] as string[], bootId: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' };
      writeFileSync(join(dir, '10.0.0.1.bootid'), '99999999-8888-7777-6666-555555555555\n');
      await assert.rejects(applyCfgEdits(fakeRunner(state), '10.0.0.1', CRED,
        [{ key: 'radio.1.mode', value: 'managed' }],
        async () => ({ ok: true, detail: 'x' })), /rebooted since last operation/);
      assert.equal(state.commands.filter((c) => c.startsWith('sed -i')).length, 0);
    } finally {
      if (prior === undefined) delete process.env.TEMP_SITE_SNAPSHOT_DIR;
      else process.env.TEMP_SITE_SNAPSHOT_DIR = prior;
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
