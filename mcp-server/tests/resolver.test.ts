import { describe, it, expect } from 'vitest';
import { CredentialResolver } from '../src/credentials/resolver.js';
import type { CredentialStore, DeviceType } from '../src/credentials/schema.js';

function mockStore(overrides?: Partial<CredentialStore>): CredentialStore {
  return {
    version: 1,
    deviceTypes: [
      {
        deviceType: 'mikrotik-ssh',
        credentials: [
          { name: 'primary', username: 'admin', password: 'pass1', priority: 10, tags: [] },
          { name: 'fallback', username: 'admin', password: 'pass2', priority: 0, tags: [] },
        ],
        policy: { maxAttempts: 3, timeoutMs: 30000, requireReadOnly: true },
      },
      {
        deviceType: 'ubnt-uisp',
        credentials: [
          { name: 'api', username: 'admin', password: 'pass3', apiToken: 'token123', priority: 0, tags: [] },
        ],
      },
    ],
    exactDeviceCredentials: [],
    ...overrides,
  };
}

describe('CredentialResolver', () => {
  it('resolves device type to sorted credentials', () => {
    const resolver = new CredentialResolver(mockStore());
    const creds = resolver.resolveByType('mikrotik-ssh');

    expect(creds).toHaveLength(2);
    expect(creds[0].name).toBe('primary');
    expect(creds[1].name).toBe('fallback');
  });

  it('returns empty array for unknown device type', () => {
    const resolver = new CredentialResolver(mockStore());
    const creds = resolver.resolveByType('cisco-ios' as DeviceType);
    expect(creds).toHaveLength(0);
  });

  it('returns only matching device type credentials', () => {
    const resolver = new CredentialResolver(mockStore());
    const creds = resolver.resolveByType('ubnt-uisp');

    expect(creds).toHaveLength(1);
    expect(creds[0].apiToken).toBe('token123');
  });

  it('resolves only the exact device and host binding without type-wide fallback', () => {
    const resolver = new CredentialResolver(mockStore({
      exactDeviceCredentials: [
        { deviceType: 'ubnt-airos', deviceId: 'uisp-123', host: '192.0.2.10', credential: { name: 'cpe-123', username: 'ubnt', password: 'unique-secret', priority: 0, tags: [] } },
        { deviceType: 'ubnt-airos', deviceId: 'uisp-456', host: '192.0.2.11', credential: { name: 'cpe-456', username: 'ubnt', password: 'other-secret', priority: 0, tags: [] } },
      ],
    }));

    expect(resolver.resolveExactDevice('ubnt-airos', ' UISP-123 ', '192.0.2.10')?.name).toBe('cpe-123');
    expect(resolver.resolveExactDevice('ubnt-airos', 'uisp-123', '192.0.2.11')).toBeNull();
    expect(resolver.resolveExactDevice('ubnt-airos', 'unknown', '192.0.2.10')).toBeNull();
  });

  it('fails closed on duplicate exact-device bindings', () => {
    const binding = { deviceType: 'ubnt-airos' as const, deviceId: 'mac-1', host: '192.0.2.20', credential: { name: 'cpe', username: 'ubnt', password: 'secret', priority: 0, tags: [] } };
    const resolver = new CredentialResolver(mockStore({ exactDeviceCredentials: [binding, binding] }));
    expect(resolver.resolveExactDevice('ubnt-airos', 'mac-1', '192.0.2.20')).toBeNull();
  });

  it('returns policies for device type', () => {
    const resolver = new CredentialResolver(mockStore());
    const policy = resolver.getPolicy('mikrotik-ssh');

    expect(policy?.maxAttempts).toBe(3);
    expect(policy?.requireReadOnly).toBe(true);
  });

  it('lists all available device types', () => {
    const resolver = new CredentialResolver(mockStore());
    const types = resolver.listDeviceTypes();
    expect(types).toContain('mikrotik-ssh');
    expect(types).toContain('ubnt-uisp');
  });
});
