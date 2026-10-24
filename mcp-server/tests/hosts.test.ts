import { describe, it, expect, afterEach } from 'vitest';
import {
  HostListError, checkReadHost, hostEnvProblems, hostMatches, hostSyntaxError, parseHostList, parseIPv4, parseIPv6,
} from '../src/security/hosts.js';
import { isHostAllowed, checkHostAllowlist } from '../src/temp-site/allowlist.js';
import { checkRouterGates, checkMtGates } from '../src/temp-site/mikrotik.js';
import { checkTransportGates } from '../src/temp-site/radio.js';
import { checkInfraWriteGates } from '../src/writes/routeros-writes.js';

const V4 = { allowIPv6: false, allowNames: false, allowBroad: false };

describe('strict IPv4 parsing', () => {
  it('accepts canonical dotted quads only', () => {
    expect(parseIPv4('10.1.2.3')).toBe(0x0a010203);
    expect(parseIPv4('0.0.0.0')).toBe(0);
    for (const bad of ['010.1.2.3', '10.01.2.3', '10.1.2', '10.1.2.3.4', '256.1.1.1', '1.1.1.1 ', ' 1.1.1.1', '1.1.1.1\n', '0x0a.1.2.3', '10.1.2.3/8', '']) {
      expect(parseIPv4(bad), JSON.stringify(bad)).toBeNull();
    }
  });
  it('rejects leading-zero octets in allowlist checks (octal to libc)', () => {
    expect(isHostAllowed('010.1.2.3', '10.0.0.0/8')).toBe(false);
    expect(isHostAllowed('8.1.2.3', '10.0.0.0/8')).toBe(false); // secret-scan:allow(address fixture for host-range classification test)
    expect(() => isHostAllowed('10.1.2.3', '010.1.2.3')).toThrow(HostListError);
    expect(() => isHostAllowed('10.1.2.3', '010.0.0.0/8')).toThrow(HostListError);
  });
  it('parses IPv6 literals without zone ids or embedded IPv4', () => {
    expect(parseIPv6('::1')).toBe(1n);
    expect(parseIPv6('fe80::1')).not.toBeNull();
    expect(parseIPv6('fe80::1%eth0')).toBeNull();
    expect(parseIPv6('::ffff:10.0.0.1')).toBeNull();
    expect(parseIPv6('1:2:3:4:5:6:7:8:9')).toBeNull();
  });
});

describe('CIDR allowlist parsing fails closed', () => {
  it('"10.0.0.0/" no longer parses as /0 (allow everything)', () => {
    expect(() => parseHostList('10.0.0.0/', V4)).toThrow(/malformed CIDR prefix/);
    expect(() => isHostAllowed('8.8.8.8', '10.0.0.0/')).toThrow(HostListError);
  });
  it('rejects malformed prefixes and entries', () => {
    for (const bad of ['10.0.0.0/abc', '10.0.0.0/-1', '10.0.0.0/ 8', '10.0.0.0/008', '10.0.0.0/33', '10.0.0.0//8', '10.0.0/8', 'garbage', '10.0.0.0/8/9', '*']) {
      expect(() => parseHostList(bad, V4), bad).toThrow(HostListError);
    }
  });
  it('refuses /0../7 unless ALLOW_BROAD_CIDR=1', () => {
    for (const broad of ['0.0.0.0/0', '10.0.0.0/7', '0.0.0.0/1']) {
      expect(() => parseHostList(broad, V4), broad).toThrow(/broader than \/8/);
    }
    expect(parseHostList('10.0.0.0/8', V4)).toHaveLength(1);
    const entries = parseHostList('0.0.0.0/0', { ...V4, allowBroad: true });
    expect(hostMatches(entries, '8.8.8.8')).toBe(true);
  });
  it('ignores empty items but nothing else', () => {
    expect(parseHostList(' 10.0.0.1 , ,10.0.0.0/24,', V4)).toHaveLength(2);
    expect(parseHostList(undefined, V4)).toHaveLength(0);
  });
});

describe('gates turn malformed allowlists into blocked results', () => {
  const saved = { ...process.env };
  afterEach(() => {
    for (const k of ['TEMP_SITE_WRITE_ENABLED', 'TEMP_SITE_ROUTEROS_TRANSPORT', 'TEMP_SITE_MT_TRANSPORT', 'TEMP_SITE_AIROS_TRANSPORT', 'TEMP_SITE_ALLOWED_HOSTS', 'NOC_WRITE_ENABLED', 'NOC_WRITE_ROUTEROS_TRANSPORT', 'NOC_WRITE_ALLOWED_HOSTS', 'ALLOW_BROAD_CIDR']) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  });
  it('"10.0.0.0/" blocks every temp-site and infra gate instead of allowing all', () => {
    process.env.TEMP_SITE_WRITE_ENABLED = 'true';
    process.env.TEMP_SITE_ROUTEROS_TRANSPORT = 'verified';
    process.env.TEMP_SITE_MT_TRANSPORT = 'verified';
    process.env.TEMP_SITE_AIROS_TRANSPORT = 'verified';
    process.env.TEMP_SITE_ALLOWED_HOSTS = '10.0.0.0/';
    process.env.NOC_WRITE_ENABLED = 'true';
    process.env.NOC_WRITE_ROUTEROS_TRANSPORT = 'verified';
    process.env.NOC_WRITE_ALLOWED_HOSTS = '10.0.0.0/';
    for (const gate of [checkRouterGates, checkMtGates, checkTransportGates, checkInfraWriteGates]) {
      const r = gate('8.8.8.8');
      expect(r.ok).toBe(false);
      expect(r.ok === false && r.error).toMatch(/malformed/);
    }
  });
  it('broad CIDRs need ALLOW_BROAD_CIDR=1', () => {
    expect(checkHostAllowlist('1.2.3.4', '0.0.0.0/0', 'X').ok).toBe(false); // secret-scan:allow(address fixture for host-range classification test)
    process.env.ALLOW_BROAD_CIDR = '1';
    expect(checkHostAllowlist('1.2.3.4', '0.0.0.0/0', 'X').ok).toBe(true); // secret-scan:allow(address fixture for host-range classification test)
  });
  it('a leading-zero host never passes a gate', () => {
    process.env.TEMP_SITE_WRITE_ENABLED = 'true';
    process.env.TEMP_SITE_ROUTEROS_TRANSPORT = 'verified';
    process.env.TEMP_SITE_ALLOWED_HOSTS = '10.0.0.0/8';
    expect(checkRouterGates('010.1.2.3').ok).toBe(false);
    expect(checkRouterGates('10.1.2.3').ok).toBe(true);
  });
  it('hostEnvProblems reports malformed host lists for startup warnings', () => {
    expect(hostEnvProblems({ TEMP_SITE_ALLOWED_HOSTS: '10.0.0.0/', NOC_READ_ALLOWED_HOSTS: '10.0.0.0/8' })).toEqual([
      expect.stringContaining('TEMP_SITE_ALLOWED_HOSTS'),
    ]);
    expect(hostEnvProblems({})).toEqual([]);
  });
});

describe('read-host policy (NOC_READ_ALLOWED_HOSTS)', () => {
  it('defaults to private, loopback and link-local space', () => {
    for (const ok of ['10.1.2.3', '172.16.0.1', '172.31.255.254', '192.168.1.1', '169.254.1.1', '127.0.0.1', '::1', 'fe80::1', 'fd12:3456::1']) { // secret-scan:allow(address fixture for host-range classification test)
      expect(checkReadHost(ok, {}).ok, ok).toBe(true);
    }
    for (const bad of ['8.8.8.8', '172.32.0.1', '192.169.0.1', '11.0.0.1', '2001:db8::1', '0.0.0.0', 'attacker.example.com']) { // secret-scan:allow(address fixture for host-range classification test)
      expect(checkReadHost(bad, {}).ok, bad).toBe(false);
    }
  });
  it('rejects non-canonical and malformed hosts before the policy', () => {
    for (const bad of ['010.1.2.3', '10.1.2.3 ', '10.1.2.3;reboot', '10.1.2.3\n', '', '[::1]', 'a b', '10.1.2']) {
      expect(checkReadHost(bad, {}).ok, JSON.stringify(bad)).toBe(false);
    }
  });
  it('an explicit list replaces the default; hostnames only when listed verbatim', () => {
    const env = { NOC_READ_ALLOWED_HOSTS: '203.0.113.0/24, tower.example.net, 10.9.9.9' }; // secret-scan:allow(address fixture for host-range classification test)
    expect(checkReadHost('203.0.113.50', env).ok).toBe(true);
    expect(checkReadHost('TOWER.example.net', env).ok).toBe(true);
    expect(checkReadHost('10.9.9.9', env).ok).toBe(true); // secret-scan:allow(address fixture for host-range classification test)
    expect(checkReadHost('10.1.1.1', env).ok).toBe(false); // default no longer applies secret-scan:allow(address fixture for host-range classification test)
    expect(checkReadHost('other.example.net', env).ok).toBe(false);
  });
  it('a malformed list blocks everything with a clear error (fail closed)', () => {
    const r = checkReadHost('10.1.1.1', { NOC_READ_ALLOWED_HOSTS: '10.0.0.0/' }); // secret-scan:allow(address fixture for host-range classification test)
    expect(r.ok).toBe(false);
    expect(r.ok === false && r.error).toMatch(/NOC_READ_ALLOWED_HOSTS is malformed/);
    expect(checkReadHost('1.1.1.1', { NOC_READ_ALLOWED_HOSTS: '0.0.0.0/0' }).ok).toBe(false);
    expect(checkReadHost('1.1.1.1', { NOC_READ_ALLOWED_HOSTS: '0.0.0.0/0', ALLOW_BROAD_CIDR: '1' }).ok).toBe(true);
  });
  it('hostSyntaxError accepts hostnames and rejects metacharacters', () => {
    expect(hostSyntaxError('router-1.example.com')).toBeNull();
    expect(hostSyntaxError('8.8.8.8 ; /user add')).not.toBeNull();
    expect(hostSyntaxError('a_b.example.com')).not.toBeNull();
  });
});
