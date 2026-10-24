import { describe, it, expect } from 'vitest';
import {
  opToEdits,
  parseEnvWpaKeys,
  validatePsk,
  resolveStationPsk,
  redactCfgValues,
  planTempRadio,
} from '../src/temp-site/radio.js';
import { restoreSnapshot, type SshRunner } from '../src/temp-site/airos-ssh.js';
import type { DeviceTypeCredential } from '../src/credentials/schema.js';

const CRED = { name: 't', username: 'ubnt', password: 'x', priority: 0, tags: [] } as DeviceTypeCredential;
const MANAGED_OP = {
  op: 'set-mode', incidentId: 'TEST', host: '10.0.0.2', deviceId: 'd', mode: 'managed', ssid: 'target5s',
} as const;

describe('station PSK edits', () => {
  it('managed without psk keeps the open profile (open APs only)', () => {
    const edits = opToEdits({ ...MANAGED_OP });
    expect(edits.map((e) => e.key)).not.toContain('wpasupplicant.profile.1.network.1.psk');
    expect(edits).toHaveLength(4);
  });

  it('managed with psk adds key + WPA-PSK mgmt', () => {
    const edits = opToEdits({ ...MANAGED_OP }, 's3cretKey');
    const byKey = new Map(edits.map((e) => [e.key, e.value]));
    expect(byKey.get('wpasupplicant.profile.1.network.1.psk')).toBe('s3cretKey');
    expect(byKey.get('wpasupplicant.profile.1.network.1.key_mgmt.1.name')).toBe('WPA-PSK');
  });

  it('master never carries psk edits', () => {
    const edits = opToEdits({ op: 'set-mode', incidentId: 'TEST', host: 'h', deviceId: 'd', mode: 'master', ssid: 'x' }, 's3cretKey');
    expect(edits.map((e) => e.key)).not.toContain('wpasupplicant.profile.1.network.1.psk');
  });
});

describe('TEMP_SITE_WPA_KEYS parsing', () => {
  it('accepts a JSON array', () => {
    expect(parseEnvWpaKeys('["alpha123","beta4567"]')).toEqual(['alpha123', 'beta4567']);
  });

  it('accepts a single bare key', () => {
    expect(parseEnvWpaKeys('justonekey')).toEqual(['justonekey']);
  });

  it('empty or missing yields no keys', () => {
    expect(parseEnvWpaKeys(undefined)).toEqual([]);
    expect(parseEnvWpaKeys('   ')).toEqual([]);
  });

  it('rejects malformed JSON and non-string entries', () => {
    expect(() => parseEnvWpaKeys('["a",')).toThrow(/valid JSON/);
    expect(() => parseEnvWpaKeys('[1,2]')).toThrow(/only non-empty strings/);
  });
});

describe('psk validation', () => {
  it('accepts WPA2-range printable keys', () => {
    expect(validatePsk('abcdefgh', 't')).toBe('abcdefgh');
  });

  it('rejects short keys and shell metacharacters', () => {
    expect(() => validatePsk('short', 't')).toThrow(/8\.\.63/);
    expect(() => validatePsk('bad;key12', 't')).toThrow(/metacharacters/);
    expect(() => validatePsk('has space key1', 't')).toThrow(/8\.\.63/);
  });
});

describe('psk resolution order', () => {
  it('prefers ap-config and records source only', async () => {
    const r = await resolveStationPsk({
      apHost: '10.0.0.9',
      apRead: async () => 'aaa.1.wpa.psk=apSecret1\n',
    });
    expect(r).toEqual({ psk: 'apSecret1', source: 'ap-config' });
  });

  it('refuses to guess when the AP has no key', async () => {
    await expect(resolveStationPsk({ apHost: '10.0.0.9', apRead: async () => 'aaa.1.status=enabled\n' }))
      .rejects.toThrow(/no aaa\.1\.wpa\.psk/);
  });

  it('falls back to env index', async () => {
    process.env.TEMP_SITE_WPA_KEYS = '["firstkey1","secondkey2"]';
    try {
      const r = await resolveStationPsk({ envIndex: 1 });
      expect(r).toEqual({ psk: 'secondkey2', source: 'env:1' });
    } finally {
      delete process.env.TEMP_SITE_WPA_KEYS;
    }
  });

  it('fails closed with no source at all', async () => {
    delete process.env.TEMP_SITE_WPA_KEYS;
    await expect(resolveStationPsk({})).rejects.toThrow(/No PSK source/);
  });
});

describe('redaction', () => {
  it('strips psk values but keeps key names', () => {
    const out = redactCfgValues('radio.1.mode=managed\naaa.1.wpa.psk=supersecret\nwpasupplicant.profile.1.network.1.psk=abc123');
    expect(out).toContain('radio.1.mode=managed');
    expect(out).toContain('aaa.1.wpa.psk=<redacted>');
    expect(out).toContain('wpasupplicant.profile.1.network.1.psk=<redacted>');
    expect(out).not.toContain('supersecret');
    expect(out).not.toContain('abc123');
  });
});

describe('plan reports pskSource without resolving', () => {
  it('set-mode plan carries the source through', () => {
    const plan = planTempRadio({
      op: 'set-mode', incidentId: 'TEST', host: '10.0.0.2', deviceId: 'd',
      mode: 'managed', ssid: 'target5s',
      pskSource: { type: 'env', index: 0 },
    });
    expect(plan.valid).toBe(true);
    expect(plan.applySteps?.join('\n')).toContain('env index 0');
  });
});

describe('snapshot restore uses stdin write, never SFTP', () => {
  it('restoreSnapshot calls write with the snapshot content', async () => {
    const calls: Array<{ path: string; content: string }> = [];
    const runner = {
      exec: async () => '',
      execMany: async () => [],
      put: async () => { throw new Error('SFTP must not be used'); },
      write: async (_h: string, _c: DeviceTypeCredential, p: string, content: string) => {
        calls.push({ path: p, content });
      },
    } as unknown as SshRunner;
    await restoreSnapshot(runner, '10.0.0.5', CRED, 'wireless.1.ssid=x\n', 5000);
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe('/tmp/system.cfg');
    expect(calls[0].content).toBe('wireless.1.ssid=x\n');
  });
});
