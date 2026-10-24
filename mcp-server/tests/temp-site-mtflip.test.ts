import { describe, it, expect } from 'vitest';
import { applyMtFlip, rollbackMtFlip, checkMtGates, type RouterRun } from '../src/temp-site/mikrotik.js';

function fakeRun(state: { fields: Record<string, string>; commands: string[] }): RouterRun {
  return async (cmd: string) => {
    state.commands.push(cmd);
    const gm = cmd.match(/\/interface wireless get \S+ ([a-z-]+)/);
    if (gm) return state.fields[gm[1]] ?? '';
    if (cmd.startsWith('/interface wireless print')) {
      // ROS7 terse print omits advanced fields (rate-set, ...): snapshot must use get.
      const f = state.fields;
      return `0 name="wlan1" mode=${f.mode} ssid="${f.ssid}" frequency=${f.frequency} wireless-protocol=${f['wireless-protocol']}`;
    }
    const m = cmd.match(/^\/interface wireless set (\S+) (.+)$/);
    if (m) {
      for (const kv of m[2].matchAll(/([a-z-]+)="([^"]+)"|([a-z-]+)=(\S+)/g)) {
        const k = kv[1] || kv[3];
        const v = (kv[2] ?? kv[4]).replace(/^"|"$/g, '');
        if (k === 'mode') state.fields.mode = v;
        else if (k === 'ssid') state.fields.ssid = v;
        else if (k === 'frequency') state.fields.frequency = v;
        else if (k === 'wireless-protocol') state.fields['wireless-protocol'] = v;
        else if (k === 'rate-set') state.fields['rate-set'] = v;
      }
      return '';
    }
    return '';
  };
}

const AP_FIELDS = () => ({ mode: 'ap-bridge', ssid: 'home24o', frequency: '2467', 'wireless-protocol': 'nstreme', 'rate-set': 'configured' });

const INPUT = {
  incidentId: 'INC-MT', host: '10.0.0.9', deviceId: 'r24o',
  iface: 'wlan1', ssid: 'targetap', execute: true as const, confirmWithinSeconds: 600,
};

describe('temp-site MikroTik wireless flip', () => {
  it('applies the full station recipe and records all previous fields', async () => {
    const state = { fields: AP_FIELDS(), commands: [] as string[] };
    const r = await applyMtFlip(fakeRun(state), INPUT);
    expect(r.applied).toBe(true);
    expect(r.previous).toEqual(AP_FIELDS());
    expect(state.fields).toEqual({ mode: 'station-bridge', ssid: 'targetap', frequency: 'auto', 'wireless-protocol': 'any', 'rate-set': 'default' });
  });

  it('is idempotent when already in the full requested state', async () => {
    const state = { fields: { mode: 'station-bridge', ssid: 'targetap', frequency: 'auto', 'wireless-protocol': 'any', 'rate-set': 'default' }, commands: [] as string[] };
    const r = await applyMtFlip(fakeRun(state), INPUT);
    expect(r.applied).toBe(false);
    expect(state.commands.filter((c) => c.includes(' set ')).length).toBe(0);
  });

  it('rejects metacharacter injection in ssid', async () => {
    const state = { fields: AP_FIELDS(), commands: [] as string[] };
    await expect(applyMtFlip(fakeRun(state), { ...INPUT, ssid: 'a";reboot;#' })).rejects.toThrow(/Validation error/);
  });

  it('rolls back all recorded fields', async () => {
    const state = { fields: { mode: 'station-bridge', ssid: 'targetap', frequency: 'auto', 'wireless-protocol': 'any', 'rate-set': 'default' }, commands: [] as string[] };
    const r = await rollbackMtFlip(fakeRun(state), { host: '10.0.0.9', mode: 'ap-bridge', ssid: 'home24o', frequency: '2467', protocol: 'nstreme', rateset: 'configured' });
    expect(r.rolledBack).toBe(true);
    expect(state.fields).toEqual(AP_FIELDS());
  });

  it('gates on kill switch, transport flag, and host allowlist', () => {
    const prior = { ...process.env };
    delete process.env.TEMP_SITE_WRITE_ENABLED;
    expect(checkMtGates('10.0.0.9').ok).toBe(false);
    process.env.TEMP_SITE_WRITE_ENABLED = 'true';
    expect(checkMtGates('10.0.0.9').ok).toBe(false);
    process.env.TEMP_SITE_MT_TRANSPORT = 'verified';
    expect(checkMtGates('10.0.0.9').ok).toBe(false);
    process.env.TEMP_SITE_ALLOWED_HOSTS = '10.0.0.9';
    expect(checkMtGates('10.0.0.9').ok).toBe(true);
    process.env = prior;
  });
});

describe('temp-site AP WDS toggle', () => {
  it('enables dynamic WDS and records previous', async () => {
    const state = { fields: { mode: 'ap-bridge', ssid: 'a', frequency: '2412', 'wireless-protocol': 'any', 'rate-set': 'default', 'wds-mode': 'disabled' }, commands: [] as string[] };
    const run = async (cmd: string) => {
      state.commands.push(cmd);
      if (cmd.startsWith('/interface wireless print')) return `0 name="wlan1" wds-mode=${state.fields['wds-mode']}`;
      const m = cmd.match(/wds-mode=(dynamic|disabled)/);
      if (m) { state.fields['wds-mode'] = m[1]; return ''; }
      return '';
    };
    const { applyApWds } = await import('../src/temp-site/mikrotik.js');
    const r = await applyApWds(run, { incidentId: 'T-1', host: '10.0.0.9', deviceId: 'd', wdsMode: 'dynamic' as const, execute: true });
    expect(r.applied).toBe(true);
    expect(r.previous).toBe('disabled');
  });
});
