import { describe, it, expect } from 'vitest';
import { planTempOspf, validateTempOspf } from '../src/temp-site/ospf.js';

describe('temp-site OSPF planner (dry-run only, never executes)', () => {
  it('plans a valid /30 host with tagged apply/rollback', () => {
    const plan = planTempOspf({
      incidentId: 'INC-99',
      host: '10.0.2.189',
      interfaze: 'ether1',
      address: '10.0.28.113/30',
      network: '10.0.28.112/30',
      ospfType: 'ptp',
      cost: 1000,
      routerOsVersion: 'ros7',
    });
    expect(plan.valid).toBe(true);
    expect(plan.apply?.[0]).toContain('10.0.28.113/30');
    expect(plan.apply?.[1]).toContain('interface-template');
    expect(plan.apply?.[1]).toContain('type=ptp');
    expect(plan.tag).toContain('REMOVE-WHEN-REPAIRED');
    expect(plan.preflight?.length).toBeGreaterThan(3);
  });

  it('emits type=broadcast when the /30 lands on a bridge segment (INC-A-01)', () => {
    const plan = planTempOspf({
      incidentId: 'INC-A-01',
      host: '10.0.208.93',
      interfaze: 'bridge1',
      address: '10.0.28.77/30',
      network: '10.0.28.76/30',
      ospfType: 'broadcast',
      cost: 1000,
      routerOsVersion: 'ros7',
    });
    expect(plan.valid).toBe(true);
    expect(plan.apply?.[1]).toContain('type=broadcast');
    expect(plan.apply?.[1]).not.toContain('type=ptp');
  });

  it('fails closed when ospfType is omitted (no silent ptp default)', () => {
    const plan = planTempOspf({
      incidentId: 'INC-99',
      host: '10.0.2.189',
      interfaze: 'bridge1',
      address: '10.0.28.113/30',
      network: '10.0.28.112/30',
      cost: 1000,
      routerOsVersion: 'ros7',
    });
    expect(plan.valid).toBe(false);
    expect(plan.error ?? '').toMatch(/ospfType/);
  });

  it('rejects any ospfType other than ptp/broadcast', () => {
    expect(planTempOspf({
      incidentId: 'INC-99', host: '10.0.0.1', interfaze: 'ether1',
      address: '10.0.28.113/30', network: '10.0.28.112/30', cost: 1000,
      routerOsVersion: 'ros7', ospfType: 'nbma',
    }).valid).toBe(false);
  });

  it('rejects network/broadcast addresses and cross-network hosts', () => {
    expect(validateTempOspf({
      incidentId: 'INC-99', host: '10.0.0.1', interfaze: 'ether1',
      address: '10.0.28.112/30', network: '10.0.28.112/30', cost: 1000, routerOsVersion: 'ros7', ospfArea: 'backbone', ospfType: 'ptp',
    }).ok).toBe(false);
    expect(planTempOspf({
      incidentId: 'INC-99', host: '10.0.0.1', interfaze: 'ether1',
      address: '10.0.28.114/30', network: '10.0.28.112/30', cost: 1000, routerOsVersion: 'ros7', ospfType: 'ptp',
    }).valid).toBe(true);
    expect(planTempOspf({
      incidentId: 'INC-99', host: '10.0.0.1', interfaze: 'ether1',
      address: '10.0.28.114/30', network: '10.0.28.116/30', cost: 1000, routerOsVersion: 'ros7', ospfType: 'ptp',
    }).valid).toBe(false);
  });

  it('rejects public space and shell metacharacters', () => {
    expect(planTempOspf({
      incidentId: 'INC-99', host: '10.0.0.1', interfaze: 'ether1',
      address: '8.8.8.1/30', network: '8.8.8.0/30', cost: 1000, routerOsVersion: 'ros7', ospfType: 'ptp',
    }).valid).toBe(false);
    expect(planTempOspf({
      incidentId: 'INC;rm', host: '10.0.0.1', interfaze: 'ether1',
      address: '10.0.28.113/30', network: '10.0.28.112/30', cost: 1000, routerOsVersion: 'ros7', ospfType: 'ptp',
    }).valid).toBe(false);
  });

  it('rejects any cost other than 1000/10000 (the cost-lock review regression: cost 10)', () => {
    const base = {
      incidentId: 'INC-99', host: '10.0.0.1', interfaze: 'ether1',
      address: '10.0.28.113/30', network: '10.0.28.112/30', routerOsVersion: 'ros7' as const, ospfType: 'ptp' as const,
    };
    // Mesh costs must fail closed with a message naming the two legal values.
    for (const cost of [10, 100, 1, 999, 1001, 65535]) {
      const plan = planTempOspf({ ...base, cost });
      expect(plan.valid).toBe(false);
      expect(plan.error ?? '').toMatch(/1000.*10000|10000.*1000/);
    }
    expect(planTempOspf({ ...base, cost: 10000 }).valid).toBe(true);
    // Omitted cost defaults to 1000 in the emitted commands.
    const def = planTempOspf({ ...base });
    expect(def.valid).toBe(true);
    expect(def.apply?.[1]).toContain('cost=1000');
  });
});
