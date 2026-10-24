import { describe, it, expect } from 'vitest';
import { RouterOsWriteSchema, planRouterOsWrite, checkInfraWriteGates } from '../src/writes/routeros-writes.js';

const base = { host: '10.0.1.5', deviceId: 'dev-1', interfaze: 'ether3', reason: 'hung radio power-cycle after firmware crash', execute: false };

describe('routeros-write planner', () => {
  it('plans a poe-cycle with off/dwell/on and read-back verify', () => {
    const parsed = RouterOsWriteSchema.parse({ ...base, op: 'poe-cycle' });
    const plan = planRouterOsWrite(parsed);
    expect(plan.valid).toBe(true);
    expect(plan.ops[0].cmd).toContain('poe-out=off');
    expect(plan.ops[1].cmd).toMatch(/^:delay \d+s$/);
    expect(plan.ops[2].cmd).toContain('poe-out=forced-on');
    expect(plan.verifyCmd).toContain('ethernet monitor');
  });

  it('shell metacharacters in interface names are rejected', () => {
    expect(RouterOsWriteSchema.safeParse({ ...base, op: 'poe-off', interfaze: 'ether1"; reboot' }).success).toBe(false);
  });

  it('interface-disable refuses uplink-looking ports and plans plain ones', () => {
    const uplink = planRouterOsWrite(RouterOsWriteSchema.parse({ ...base, op: 'interface-disable', interfaze: 'ether1-uplink' }));
    expect(uplink.valid).toBe(false);
    const plain = planRouterOsWrite(RouterOsWriteSchema.parse({ ...base, op: 'interface-disable', interfaze: 'ether4' }));
    expect(plain.valid).toBe(true);
    expect(plain.ops[0].cmd).toContain('/interface disable');
  });

  it('gates block until kill switch + transport verify + allowlist satisfied', () => {
    const prev = process.env;
    process.env = { ...prev, NOC_WRITE_ENABLED: undefined, NOC_WRITE_ROUTEROS_TRANSPORT: undefined, NOC_WRITE_ALLOWED_HOSTS: undefined };
    expect(checkInfraWriteGates('10.0.1.5').ok).toBe(false);
    process.env.NOC_WRITE_ENABLED = 'true';
    expect(checkInfraWriteGates('10.0.1.5').ok).toBe(false);
    process.env.NOC_WRITE_ROUTEROS_TRANSPORT = 'verified';
    expect(checkInfraWriteGates('10.0.1.5').ok).toBe(false);
    process.env.NOC_WRITE_ALLOWED_HOSTS = '10.0.1.5,10.0.2.0/24';
    expect(checkInfraWriteGates('10.0.1.5').ok).toBe(true);
    expect(checkInfraWriteGates('10.1.2.3').ok).toBe(false);
    process.env = prev;
  });
});
