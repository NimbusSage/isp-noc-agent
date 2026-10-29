import { describe, it, expect } from 'vitest';
import { planTempRadio, checkExecuteGates, opToEdits, readbackCommand, redactCfgValues } from '../src/temp-site/radio.js';

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

describe('readbackCommand', () => {
  const ops = [
    { op: 'set-mode', mode: 'managed', ssid: 'TEST-AP' },
    { op: 'set-mode', mode: 'master' },
    { op: 'set-frequency', frequencyMhz: 5180, chanbw: 20 },
  ] as const;

  it('reads back every key the op edits, for every op shape', () => {
    for (const op of ops) {
      const edits = opToEdits(op as never, op.op === 'set-mode' && op.mode === 'managed' ? 'a-valid-test-psk' : undefined);
      const cmd = readbackCommand(edits);
      const wanted = cmd.match(/\^\((.+)\)=/)?.[1].split('|') ?? [];
      expect(wanted).toEqual(edits.map((e) => e.key));
      expect(cmd.endsWith(' /tmp/system.cfg')).toBe(true);
    }
  });

  it('covers the wpasupplicant and chanbw keys the old fixed grep missed', () => {
    const managed = readbackCommand(opToEdits({ op: 'set-mode', mode: 'managed', ssid: 'TEST-AP' } as never));
    expect(managed).toContain('wpasupplicant.profile.1.network.1.ssid');
    expect(readbackCommand(opToEdits({ op: 'set-frequency', frequencyMhz: 5180, chanbw: 20 } as never))).toContain('radio.1.chanbw');
  });

  it('refuses keys that could break out of the quoted pattern, and an empty edit list', () => {
    for (const key of ["radio.1.mode'; id #", 'a b', 'a|b', '', 'x$(id)']) {
      expect(() => readbackCommand([{ key, value: 'v' }])).toThrow(/unsafe/);
    }
    expect(() => readbackCommand([])).toThrow(/no edits/);
  });

  it('a key line that comes back with the PSK is redacted before it reaches any detail text', () => {
    const readback = 'radio.1.mode=managed\nwpasupplicant.profile.1.network.1.psk=super-secret-test-value';
    expect(redactCfgValues(readback)).not.toContain('super-secret-test-value');
  });
});
