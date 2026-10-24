import { describe, it, expect } from 'vitest';
import { applyOspfAdjacency, rollbackOspfAdjacency, checkRouterGates, type RouterRun } from '../src/temp-site/mikrotik.js';

function fakeRun(state: { addr: string[]; ospf: string[]; commands: string[] }): RouterRun {
  return async (cmd: string) => {
    state.commands.push(cmd);
    // Mimic live ROS: `where address=X/Y` (CIDR) never matches; comment tag does.
    if (cmd.startsWith('/ip address print where address=')) return '';
    if (cmd.startsWith('/ip address print where comment=')) return state.addr.join('\n');
    if (cmd.startsWith('/ip address add')) {
      const m = cmd.match(/address=([0-9./]+) interface=([^\s]+)/);
      if (m) state.addr.push(`${m[1]} ${m[2]}`);
      return '';
    }
    if (cmd.includes('interface-template add') || cmd.includes('ospf interface add')) {
      const m = cmd.match(/cost=(\d+)/);
      const n = cmd.match(/networks=([0-9./]+)/);
      const t = cmd.match(/type=(ptp|broadcast)/);
      if (m) state.ospf.push(`cost=${m[1]} ${n ? n[0] : ''} ${t ? t[0] : ''}`);
      return '';
    }
    if (cmd.includes('print where comment=')) return [...state.addr, ...state.ospf].join('\n');
    if (cmd.includes('remove [find')) { state.addr = []; state.ospf = []; return ''; }
    return '';
  };
}

const INPUT = {
  incidentId: 'INC-9', host: '10.0.0.1', deviceId: 'r1',
  address: '10.0.28.113/30', network: '10.0.28.112/30', interfaze: 'ether1',
  ospfArea: 'backbone', ospfType: 'ptp' as const, cost: 1000, routerOsVersion: 'ros7' as const, execute: true,
};

describe('temp-site RouterOS executor', () => {
  it('applies and verifies the /30 + OSPF adjacency', async () => {
    const state = { addr: [] as string[], ospf: [] as string[], commands: [] as string[] };
    const r = await applyOspfAdjacency(fakeRun(state), INPUT);
    expect(r.applied).toBe(true);
    expect(r.rolledBack).toBe(false);
    expect(r.tag).toContain('REMOVE-WHEN-REPAIRED');
    expect(state.commands.some((c) => c.includes('interface-template add'))).toBe(true);
  });

  it('refuses when the address already exists', async () => {
    const state = { addr: ['10.0.28.113/30 ether1 TEMP-INC-9-REMOVE-WHEN-REPAIRED'], ospf: [] as string[], commands: [] as string[] };
    await expect(applyOspfAdjacency(fakeRun(state), INPUT)).rejects.toThrow(/already mentions/);
    expect(state.commands.some((c) => c.includes('interface-template add'))).toBe(false);
  });

  it('treats a CIDR where-filter miss as empty and verifies by tag instead', async () => {
    // Regression for the live failure: `where address=X/Y` never
    // matches on ROS, so verification must use the comment tag.
    const state = { addr: [] as string[], ospf: [] as string[], commands: [] as string[] };
    const run = fakeRun(state);
    expect(await run('/ip address print where address=10.0.28.113/30')).toBe('');
    const r = await applyOspfAdjacency(run, INPUT);
    expect(r.applied).toBe(true);
  });

  it('emits the caller-declared ospfType and verifies it on readback', async () => {
    const state = { addr: [] as string[], ospf: [] as string[], commands: [] as string[] };
    const r = await applyOspfAdjacency(fakeRun(state), { ...INPUT, interfaze: 'bridge1', ospfType: 'broadcast' as const });
    expect(r.applied).toBe(true);
    expect(state.commands.some((c) => c.includes('type=broadcast'))).toBe(true);
    expect(state.commands.some((c) => c.includes('type=ptp'))).toBe(false);
  });

  it('fails closed when ospfType is omitted (no silent ptp default)', async () => {
    const state = { addr: [] as string[], ospf: [] as string[], commands: [] as string[] };
    const { ospfType: _drop, ...noType } = INPUT;
    await expect(applyOspfAdjacency(fakeRun(state), noType)).rejects.toThrow(/ospfType/);
    expect(state.commands.some((c) => c.includes('interface-template add'))).toBe(false);
  });

  it('rejects a mesh OSPF cost at apply time (the cost-lock review regression: cost 10)', async () => {
    const state = { addr: [] as string[], ospf: [] as string[], commands: [] as string[] };
    await expect(applyOspfAdjacency(fakeRun(state), { ...INPUT, cost: 10 })).rejects.toThrow(/1000.*10000|10000.*1000/);
    expect(state.commands.some((c) => c.includes('interface-template add'))).toBe(false);
  });

  it('rolls back and verifies removal', async () => {
    const state = { addr: ['x TEMP-INC-9-REMOVE-WHEN-REPAIRED'], ospf: [] as string[], commands: [] as string[] };
    const r = await rollbackOspfAdjacency(fakeRun(state), 'TEMP-INC-9-REMOVE-WHEN-REPAIRED', 'ros7');
    expect(r.rolledBack).toBe(true);
  });

  it('gates on kill switch, transport flag, and host allowlist', () => {
    const prior = { ...process.env };
    delete process.env.TEMP_SITE_WRITE_ENABLED;
    expect(checkRouterGates('10.0.0.1').ok).toBe(false);
    process.env.TEMP_SITE_WRITE_ENABLED = 'true';
    expect(checkRouterGates('10.0.0.1').ok).toBe(false);
    process.env.TEMP_SITE_ROUTEROS_TRANSPORT = 'verified';
    expect(checkRouterGates('10.0.0.1').ok).toBe(false);
    process.env.TEMP_SITE_ALLOWED_HOSTS = '10.0.0.1';
    expect(checkRouterGates('10.0.0.1').ok).toBe(true);
    expect(checkRouterGates('10.0.0.2').ok).toBe(false);
    process.env = prior;
  });
});
