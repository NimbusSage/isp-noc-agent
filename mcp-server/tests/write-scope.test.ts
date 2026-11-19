import { describe, it, expect } from 'vitest';
import { checkWriteHost, DEFAULT_WRITE_ALLOWED_HOSTS, hostEnvProblems } from '../src/security/hosts.js';

const env = (over: Record<string, string | undefined> = {}) => ({ ...over }) as NodeJS.ProcessEnv;

describe('write scope (checkWriteHost)', () => {
  it('allows private, link-local and loopback IPv4 with no configuration at all', () => {
    for (const host of ['10.0.1.5', '10.1.2.9', '192.168.1.2', '169.254.10.1', '127.0.0.1']) {
      expect(checkWriteHost(host, env()).ok, host).toBe(true);
    }
  });

  it('blocks public addresses by default, including one the operator might name', () => {
    for (const host of ['8.8.8.8', '203.0.113.7']) {
      const r = checkWriteHost(host, env());
      expect(r.ok, host).toBe(false);
      if (!r.ok) expect(r.error).toMatch(/default write scope/);
    }
  });

  it('rejects a malformed host before any network scope check', () => {
    expect(checkWriteHost('010.0.0.5', env()).ok).toBe(false);
    expect(checkWriteHost('10.0.0.5; reboot', env()).ok).toBe(false);
  });

  it('lets an explicit list replace the default, narrowing or extending it', () => {
    expect(checkWriteHost('10.0.1.5', env({ NOC_WRITE_ALLOWED_HOSTS: '10.0.2.0/24' })).ok).toBe(false);
    expect(checkWriteHost('10.0.2.9', env({ NOC_WRITE_ALLOWED_HOSTS: '10.0.2.0/24' })).ok).toBe(true);
    expect(checkWriteHost('203.0.113.7', env({ NOC_WRITE_ALLOWED_HOSTS: '203.0.113.0/24' })).ok).toBe(true);
    expect(checkWriteHost('10.0.1.5', env({ NOC_WRITE_ALLOWED_HOSTS: '203.0.113.0/24' })).ok).toBe(false);
  });

  it('fails closed on a malformed list instead of silently widening', () => {
    const r = checkWriteHost('10.0.1.5', env({ NOC_WRITE_ALLOWED_HOSTS: '10.0.0.0/' }));
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toMatch(/malformed/);
    // Startup self-check still reports it.
    expect(hostEnvProblems(env({ NOC_WRITE_ALLOWED_HOSTS: '10.0.0.0/' }))).toHaveLength(1);
  });

  it('keeps the default list to IPv4 private ranges only', () => {
    expect(DEFAULT_WRITE_ALLOWED_HOSTS.split(',')).toEqual([
      '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '127.0.0.0/8',
    ]);
  });
});
