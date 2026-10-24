import { describe, it, expect } from 'vitest';
import { planTempRadio, checkExecuteGates } from '../src/temp-site/radio.js';

describe('temp-site radio planner (dry-run only)', () => {
  it('plans a station flip with verify + rollback and a confirm deadline', () => {
    const plan = planTempRadio({ op: 'set-mode', incidentId: 'INC-7', host: '10.0.84.11', deviceId: 'airos-ap-1', mode: 'managed', ssid: 'test-ssid-5w' });
    expect(plan.valid).toBe(true);
    expect(plan.tag).toContain('REMOVE-WHEN-REPAIRED');
    expect(plan.verifySteps?.length).toBeGreaterThan(2);
    expect(plan.rollbackSteps?.length).toBeGreaterThan(0);
    expect(plan.executes).toBe(false);
    expect(plan.confirmBy).toBeTruthy();
  });

  it('plans a frequency change with AUTO-previous restore rule', () => {
    const plan = planTempRadio({ op: 'set-frequency', incidentId: 'INC-7', host: '10.0.84.11', deviceId: 'airos-ap-1', frequencyMhz: 5295, chanbw: 20, previousFrequencyMhz: 0 });
    expect(plan.valid).toBe(true);
    expect(plan.rollbackSteps?.join(' ')).toContain('never AUTO');
  });

  it('rejects shell metacharacters, bad hosts, and out-of-band frequencies', () => {
    expect(planTempRadio({ op: 'set-mode', incidentId: 'INC;rm', host: '10.0.84.11', deviceId: 'x', mode: 'managed', ssid: 's' }).valid).toBe(false);
    expect(planTempRadio({ op: 'reboot', incidentId: 'INC-7', host: 'not-an-ip', deviceId: 'x' }).valid).toBe(false);
    expect(planTempRadio({ op: 'set-frequency', incidentId: 'INC-7', host: '10.0.84.11', deviceId: 'x', frequencyMhz: 2400, previousFrequencyMhz: 5295 }).valid).toBe(false);
    expect(planTempRadio({ op: 'dance', incidentId: 'INC-7' }).valid).toBe(false);
  });

  it('blocks execution unless the kill switch is explicitly on', () => {
    const prior = process.env.TEMP_SITE_WRITE_ENABLED;
    delete process.env.TEMP_SITE_WRITE_ENABLED;
    expect(checkExecuteGates().ok).toBe(false);
    process.env.TEMP_SITE_WRITE_ENABLED = 'true';
    expect(checkExecuteGates().ok).toBe(true);
    if (prior === undefined) delete process.env.TEMP_SITE_WRITE_ENABLED;
    else process.env.TEMP_SITE_WRITE_ENABLED = prior;
  });
});
