import { describe, it, expect } from 'vitest';
import { createDeviceClient } from '../src/devices/factory.js';
import { MikroTikClient } from '../src/devices/mikrotik.js';
import { UBNTUispClient } from '../src/devices/ubnt-uisp.js';
import { UBNTAirosClient } from '../src/devices/ubnt-airos.js';
import type { DeviceTypeCredential, DeviceType } from '../src/credentials/schema.js';

function makeCredential(): DeviceTypeCredential {
  return { name: 'test', username: 'admin', password: 'pass', priority: 0, tags: [] };
}

describe('createDeviceClient', () => {
  it('creates MikroTikClient for mikrotik-ssh', () => {
    const client = createDeviceClient('10.0.0.1', makeCredential(), 'mikrotik-ssh');
    expect(client).toBeInstanceOf(MikroTikClient);
  });

  it('creates MikroTikClient for mikrotik-rest', () => {
    const client = createDeviceClient('10.0.0.1', makeCredential(), 'mikrotik-rest');
    expect(client).toBeInstanceOf(MikroTikClient);
  });

  it('creates UBNTUispClient for ubnt-uisp', () => {
    const client = createDeviceClient('10.0.0.1', makeCredential(), 'ubnt-uisp');
    expect(client).toBeInstanceOf(UBNTUispClient);
  });

  it('creates UBNTAirosClient for ubnt-airos', () => {
    const client = createDeviceClient('10.0.0.1', makeCredential(), 'ubnt-airos');
    expect(client).toBeInstanceOf(UBNTAirosClient);
  });

  it('throws for unknown device type', () => {
    expect(() => createDeviceClient('10.0.0.1', makeCredential(), 'cisco-ios' as DeviceType)).toThrow('Unknown device type');
  });
});
