import type { DeviceTypeCredential, DeviceType } from '../credentials/schema.js';
import type { DeviceClient } from './types.js';
import { MikroTikClient } from './mikrotik.js';
import { UBNTUispClient } from './ubnt-uisp.js';
import { UBNTAirosClient } from './ubnt-airos.js';
import { UBNTWaveClient } from './ubnt-wave.js';

const DEFAULT_PORTS: Record<DeviceType, number> = {
  'mikrotik-ssh': 22,
  'mikrotik-rest': 80,
  'ubnt-uisp': 443,
  'ubnt-airos': 80,
  'ubnt-wave': 22,
  'customer-airos': 80,
  'customer-mt': 22,
};

export function createDeviceClient(
  host: string,
  credential: DeviceTypeCredential,
  deviceType: DeviceType,
  timeoutMs?: number,
): DeviceClient {
  const port = DEFAULT_PORTS[deviceType];
  switch (deviceType) {
    case 'mikrotik-ssh':
    case 'customer-mt':
    case 'mikrotik-rest':
      return new MikroTikClient(host, port, credential, timeoutMs);
    case 'ubnt-uisp':
      return new UBNTUispClient(host, port, credential, timeoutMs);
    case 'ubnt-airos':
    case 'customer-airos':
      return new UBNTAirosClient(host, port, credential, timeoutMs);
    case 'ubnt-wave':
      return new UBNTWaveClient(host, port, credential, timeoutMs);
    default:
      throw new Error(`Unknown device type: ${deviceType}`);
  }
}
