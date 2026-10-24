import { describe, it, expect } from 'vitest';
import { applyMtAlias, removeMtAlias, type RouterRun } from '../src/temp-site/mikrotik.js';

function fakeRun(state: { addrs: Set<string>; commands: string[]; hideAfterAdd?: boolean }): RouterRun {
  return async (cmd: string) => {
    state.commands.push(cmd);
    if (cmd === '/ip address print') {
      const rows = [...state.addrs].map(
        (a) => `0 address=${a} interface=bridge1 comment="TEMP-INC-MTALIAS-REMOVE-WHEN-REPAIRED"`,
      );
      if (state.hideAfterAdd) return '';
      return rows.join('\n');
    }
    const m = cmd.match(/^\/ip address add address=(\S+) interface=(\S+) comment="([^"]+)"$/);
    if (m) {
      state.addrs.add(m[1]);
      return '';
    }
    const r = cmd.match(/^\/ip address remove \[find comment="([^"]+)"\]$/);
    if (r) {
      for (const a of [...state.addrs]) state.addrs.delete(a);
      return '';
    }
    return '';
  };
}

const INPUT = {
  incidentId: 'INC-MTALIAS', host: '10.0.0.9', deviceId: 'r24o',
  address: '10.0.20.60/24', interfaze: 'bridge1', execute: true as const,
};

describe('temp-site MikroTik alias', () => {
  it('adds the alias and verifies readback', async () => {
    const state = { addrs: new Set<string>(), commands: [] as string[] };
    const r = await applyMtAlias(fakeRun(state), INPUT);
    expect(r.applied).toBe(true);
    expect(state.addrs.has('10.0.20.60/24')).toBe(true);
  });

  it('is idempotent when the address is already present', async () => {
    const state = { addrs: new Set<string>(['10.0.20.60/24']), commands: [] as string[] };
    const r = await applyMtAlias(fakeRun(state), INPUT);
    expect(r.applied).toBe(false);
    expect(state.commands.filter((c) => c.includes('add address')).length).toBe(0);
  });

  it('rolls back by tag on readback miss', async () => {
    const state = { addrs: new Set<string>(), commands: [] as string[], hideAfterAdd: true };
    const r = await applyMtAlias(fakeRun(state), INPUT);
    expect(r.applied).toBe(false);
    expect(r.rolledBack).toBe(true);
    expect(state.addrs.size).toBe(0);
  });

  it('rejects bad address and metacharacters', async () => {
    const state = { addrs: new Set<string>(), commands: [] as string[] };
    await expect(applyMtAlias(fakeRun(state), { ...INPUT, address: '10.0.0.1/33' })).rejects.toThrow(/Validation error/);
    await expect(applyMtAlias(fakeRun(state), { ...INPUT, address: '10.0.0.1/24";reboot;#' })).rejects.toThrow(
      /Validation error/,
    );
  });

  it('removes the tag', async () => {
    const state = { addrs: new Set<string>(['10.0.20.60/24']), commands: [] as string[] };
    const r = await removeMtAlias(fakeRun(state), { host: '10.0.0.9', incidentId: 'INC-MTALIAS' });
    expect(r.removed).toBe(true);
  });
});
