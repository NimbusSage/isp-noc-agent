import type { CredentialStore, DeviceTypeCredential, DeviceTypeGroup, DeviceType, ExactDeviceCredential } from './schema.js';

export class CredentialResolver {
  private store: CredentialStore;

  constructor(store: CredentialStore) {
    this.store = store;
  }

  resolveByType(deviceType: DeviceType): DeviceTypeCredential[] {
    const group = this.store.deviceTypes.find(g => g.deviceType === deviceType);
    if (!group) return [];
    return [...group.credentials].sort((a, b) => b.priority - a.priority);
  }

  /**
   * Resolve exactly one device-bound credential by a canonical ID supplied by a trusted inventory.
   * Matching is case-insensitive after trimming. Ambiguous bindings fail closed. No type-wide fallback.
   */
  resolveExactDevice(deviceType: DeviceType, deviceId: string, host: string): DeviceTypeCredential | null {
    const canonicalId = deviceId.trim().toLowerCase();
    const canonicalHost = host.trim().toLowerCase();
    if (!canonicalId || !canonicalHost) return null;

    const matches: ExactDeviceCredential[] = (this.store.exactDeviceCredentials ?? []).filter(binding =>
      binding.deviceType === deviceType &&
      binding.deviceId.trim().toLowerCase() === canonicalId &&
      binding.host.trim().toLowerCase() === canonicalHost,
    );
    if (matches.length !== 1) return null;
    return matches[0].credential;
  }

  getPolicy(deviceType: DeviceType): DeviceTypeGroup['policy'] | null {
    const group = this.store.deviceTypes.find(g => g.deviceType === deviceType);
    return group?.policy ?? null;
  }

  listDeviceTypes(): DeviceType[] {
    return this.store.deviceTypes.map(g => g.deviceType);
  }
}
