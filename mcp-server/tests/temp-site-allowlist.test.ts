import { describe, it, expect, afterEach } from 'vitest';
import { isHostAllowed } from '../src/temp-site/allowlist.js';
import { checkRouterGates, checkMtGates } from '../src/temp-site/mikrotik.js';
import { checkTransportGates } from '../src/temp-site/radio.js';

describe('isHostAllowed', () => {
  it('matches exact IPs', () => {
    expect(isHostAllowed('10.0.3.22', '10.0.3.22')).toBe(true);
    expect(isHostAllowed('10.0.3.23', '10.0.3.22')).toBe(false);
  });

  it('matches CIDR ranges', () => {
    expect(isHostAllowed('10.0.174.12', '10.0.0.0/8')).toBe(true);
    expect(isHostAllowed('10.0.151.49', '10.0.0.0/8')).toBe(true);
    expect(isHostAllowed('192.168.1.5', '10.0.0.0/8')).toBe(false);
    expect(isHostAllowed('10.0.174.12', '10.0.174.0/24')).toBe(true);
    expect(isHostAllowed('10.0.175.12', '10.0.174.0/24')).toBe(false);
  });

  it('mixes exact and CIDR entries', () => {
    expect(isHostAllowed('10.0.3.22', '10.0.3.22,192.168.0.0/16')).toBe(true);
    expect(isHostAllowed('192.168.5.5', '10.0.3.22,192.168.0.0/16')).toBe(true); // secret-scan:allow(address fixture for allowlist boundary test)
    expect(isHostAllowed('172.16.0.1', '10.0.3.22,192.168.0.0/16')).toBe(false); // secret-scan:allow(address fixture for allowlist boundary test)
  });

  it('rejects invalid hosts and fails CLOSED on malformed entries (was: skipped them)', () => {
    expect(isHostAllowed('not-an-ip', '10.0.0.0/8')).toBe(false);
    expect(isHostAllowed('', '10.0.0.0/8')).toBe(false);
    expect(isHostAllowed('10.1.2.3', '')).toBe(false);
    expect(isHostAllowed('10.1.2.3', undefined)).toBe(false);
    // Behavior change (Lane 2b): one bad entry used to be skipped silently; it now
    // throws so a typo can never widen or narrow scope without anyone noticing.
    expect(() => isHostAllowed('10.1.2.3', 'garbage,10.0.0.0/8')).toThrow(/malformed/);
    expect(() => isHostAllowed('10.1.2.3', '10.0.0.0/33,10.0.0.0/8')).toThrow(/out of range/);
  });

  it('applies the mask bit-correctly at boundaries', () => {
    expect(isHostAllowed('10.0.254.254', '10.0.0.0/8')).toBe(true);
    expect(isHostAllowed('11.0.0.0', '10.0.0.0/8')).toBe(false); // secret-scan:allow(address fixture for allowlist boundary test)
    expect(isHostAllowed('10.0.174.12', '10.0.174.12/32')).toBe(true);
    expect(isHostAllowed('10.0.174.13', '10.0.174.12/32')).toBe(false);
  });
});

describe('gate checks honor CIDR', () => {
  const saved = { ...process.env };

  afterEach(() => {
    for (const k of ['TEMP_SITE_WRITE_ENABLED', 'TEMP_SITE_ROUTEROS_TRANSPORT', 'TEMP_SITE_MT_TRANSPORT', 'TEMP_SITE_AIROS_TRANSPORT', 'TEMP_SITE_ALLOWED_HOSTS']) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  });

  it('router gates pass a CIDR-covered host', () => {
    process.env.TEMP_SITE_WRITE_ENABLED = 'true';
    process.env.TEMP_SITE_ROUTEROS_TRANSPORT = 'verified';
    process.env.TEMP_SITE_ALLOWED_HOSTS = '10.0.0.0/8';
    expect(checkRouterGates('10.0.174.1')).toEqual({ ok: true });
    expect(checkRouterGates('192.168.1.1')).toEqual({ ok: false, error: expect.stringContaining('192.168.1.1') } as never);
  });

  it('mt gates pass a CIDR-covered host', () => {
    process.env.TEMP_SITE_WRITE_ENABLED = 'true';
    process.env.TEMP_SITE_MT_TRANSPORT = 'verified';
    process.env.TEMP_SITE_ALLOWED_HOSTS = '10.0.0.0/8';
    expect(checkMtGates('10.0.48.42')).toEqual({ ok: true });
  });

  it('airos gates pass a CIDR-covered host', () => {
    process.env.TEMP_SITE_WRITE_ENABLED = 'true';
    process.env.TEMP_SITE_AIROS_TRANSPORT = 'verified';
    process.env.TEMP_SITE_ALLOWED_HOSTS = '10.0.0.0/8';
    expect(checkTransportGates('10.0.84.10')).toEqual({ ok: true });
  });
});
