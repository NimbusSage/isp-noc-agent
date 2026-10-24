import { describe, it, expect } from 'vitest';
import { createDeviceClient } from '../src/devices/factory.js';
import { UBNTWaveClient } from '../src/devices/ubnt-wave.js';
import { CommandValidator } from '../src/commands/validator.js';
import { CredentialStoreSchema } from '../src/credentials/schema.js';
import type { DeviceTypeCredential } from '../src/credentials/schema.js';

function makeCredential(): DeviceTypeCredential {
  return { name: 'test', username: 'ubnt', password: 'pass', priority: 0, tags: [] };
}

describe('ubnt-wave device type', () => {
  it('creates UBNTWaveClient for ubnt-wave', () => {
    const client = createDeviceClient('10.0.0.1', makeCredential(), 'ubnt-wave');
    expect(client).toBeInstanceOf(UBNTWaveClient);
  });

  it('allows ucfg-show for ubnt-wave', () => {
    const validator = new CommandValidator();
    const result = validator.validate('ubnt-wave', 'ucfg-show', {});
    expect(result.valid).toBe(true);
  });

  it('allows assoc-stations for ubnt-wave', () => {
    const validator = new CommandValidator();
    const result = validator.validate('ubnt-wave', 'assoc-stations', {});
    expect(result.valid).toBe(true);
  });

  it('allows iwconfig for ubnt-wave', () => {
    const validator = new CommandValidator();
    const result = validator.validate('ubnt-wave', 'iwconfig', {});
    expect(result.valid).toBe(true);
  });

  it('rejects airos/mikrotik commands for ubnt-wave', () => {
    const validator = new CommandValidator();
    expect(validator.validate('ubnt-wave', 'system-resource', {}).valid).toBe(false);
    expect(validator.validate('ubnt-wave', 'status', {}).valid).toBe(false);
  });

  it('rejects wave commands for other pools', () => {
    const validator = new CommandValidator();
    expect(validator.validate('ubnt-airos', 'ucfg-show', {}).valid).toBe(false);
    expect(validator.validate('mikrotik-ssh', 'assoc-stations', {}).valid).toBe(false);
  });

  it('parses a credential store with a ubnt-wave group', () => {
    const store = CredentialStoreSchema.parse({
      version: 1,
      deviceTypes: [
        {
          deviceType: 'ubnt-wave',
          credentials: [{ name: 'wave-infra', username: 'ubnt', password: 'x', priority: 10, tags: ['infra'] }],
          policy: { maxAttempts: 5, timeoutMs: 30000, requireReadOnly: true },
        },
      ],
    });
    expect(store.deviceTypes[0].deviceType).toBe('ubnt-wave');
  });

  it('builds Wave command templates without params', () => {
    const client = new UBNTWaveClient('10.0.0.1', 22, makeCredential());
    expect(client.buildCommand({ name: 'ucfg-show', category: 'system', description: '', template: 'ucfg show', params: {}, readOnly: true }, {})).toBe('ucfg show');
  });
});
