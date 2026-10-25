import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { registeredUrl, registeredUrls, registeredHostsForDeviceType, normalizeServiceUrl } from '../src/config/resources.js';
import { checkReadHost } from '../src/security/hosts.js';

const REAL_ROOT = resolve(__dirname, '..', '..');

describe('service registry', () => {
  let dir: string;
  let env: NodeJS.ProcessEnv;

  const profile = (data: unknown): void => {
    writeFileSync(join(dir, 'site-pack', 'profile.json'), JSON.stringify(data));
  };

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'registry-'));
    mkdirSync(join(dir, 'site-pack'));
    env = { NOC_PROFILE_FILE: join(dir, 'site-pack', 'profile.json') };
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it('normalizes bare hosts and refuses credentials, queries and other schemes', () => {
    expect(normalizeServiceUrl('Uisp.Corp-Net.org/')).toBe('https://uisp.corp-net.org');
    expect(normalizeServiceUrl('https://uisp.corp-net.org:443')).toBe('https://uisp.corp-net.org');
    for (const bad of ['', 'ftp://h.corp-net.org', 'https://u:p@h.corp-net.org', 'https://h.corp-net.org/?x=1']) {  // secret-scan:allow(fixture: a URL with credentials must be refused)
      expect(normalizeServiceUrl(bad)).toBe('');
    }
  });

  it('prefers the environment, then the profile, then the older profile key', () => {
    profile({ org: { uisp_host: 'old.corp-net.org' }, resources: { uisp: { url: 'https://new.corp-net.org' } } });
    expect(registeredUrl('uisp', env, REAL_ROOT)).toBe('https://new.corp-net.org');
    expect(registeredUrls('uisp', env, REAL_ROOT)).toEqual(['https://new.corp-net.org', 'https://old.corp-net.org']);
    expect(registeredUrl('uisp', { ...env, UISP_BASE_URL: 'https://env.corp-net.org' }, REAL_ROOT)).toBe('https://env.corp-net.org');
  });

  it('is empty when nothing is registered or the profile is unreadable', () => {
    expect(registeredUrl('uisp', env, REAL_ROOT)).toBe('');
    writeFileSync(join(dir, 'site-pack', 'profile.json'), '{not json');
    expect(registeredUrl('uisp', env, REAL_ROOT)).toBe('');
  });

  it('maps a device type to the hosts of the services that use it', () => {
    profile({ resources: { uisp: { url: 'https://uisp.corp-net.org' }, librenms: { url: 'https://lnms.corp-net.org' } } });
    expect(registeredHostsForDeviceType('ubnt-uisp', env, REAL_ROOT)).toEqual(['uisp.corp-net.org']);
    expect(registeredHostsForDeviceType('mikrotik-ssh', env, REAL_ROOT)).toEqual([]);
  });

  it('the shipped catalog names the UISP device type', () => {
    const catalog = JSON.parse(readFileSync(join(REAL_ROOT, 'scripts', 'resource_catalog.json'), 'utf8')).resources;
    expect(catalog.uisp.mcp_device_type).toBe('ubnt-uisp');
  });
});

describe('read-host policy with a registered service host', () => {
  let dir: string;
  let env: NodeJS.ProcessEnv;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'registry-'));
    mkdirSync(join(dir, 'site-pack'));
    writeFileSync(join(dir, 'site-pack', 'profile.json'), JSON.stringify({ resources: { uisp: { url: 'https://uisp.corp-net.org' } } }));
    env = { NOC_PROFILE_FILE: join(dir, 'site-pack', 'profile.json') };
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  it('allows the registered UISP host name for ubnt-uisp only', () => {
    expect(checkReadHost('uisp.corp-net.org', env, 'ubnt-uisp').ok).toBe(true);
    expect(checkReadHost('UISP.corp-net.org', env, 'ubnt-uisp').ok).toBe(true);
    expect(checkReadHost('uisp.corp-net.org', env, 'mikrotik-ssh').ok).toBe(false);
    expect(checkReadHost('uisp.corp-net.org', env).ok).toBe(false);
  });

  it('refuses a host name that is not registered, and says how to register one', () => {
    const verdict = checkReadHost('other.corp-net.org', env, 'ubnt-uisp');
    expect(verdict.ok).toBe(false);
    if (!verdict.ok) expect(verdict.error).toContain('scripts/resources.py set');
  });

  it('keeps the private default and does not need NOC_READ_ALLOWED_HOSTS', () => {
    expect(checkReadHost('10.1.2.3', env, 'ubnt-uisp').ok).toBe(true);
    expect(checkReadHost('203.0.113.9', env, 'ubnt-uisp').ok).toBe(false);
  });

  it('still adds to an explicit NOC_READ_ALLOWED_HOSTS list instead of being replaced by it', () => {
    const withList = { ...env, NOC_READ_ALLOWED_HOSTS: '10.50.0.0/16' };  // secret-scan:allow(test-only private range)
    expect(checkReadHost('uisp.corp-net.org', withList, 'ubnt-uisp').ok).toBe(true);
    expect(checkReadHost('10.60.0.1', withList, 'ubnt-uisp').ok).toBe(false);  // secret-scan:allow(test-only private address outside the list)
  });
});
