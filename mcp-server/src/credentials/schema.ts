import { z } from 'zod/v3';

export const DeviceTypeSchema = z.enum(['mikrotik-ssh', 'mikrotik-rest', 'ubnt-uisp', 'ubnt-airos', 'ubnt-wave', 'customer-airos', 'customer-mt']);
export type DeviceType = z.infer<typeof DeviceTypeSchema>;

export const DeviceTypeCredentialSchema = z.object({
  name: z.string().min(1).max(64),
  username: z.string().min(1).max(64),
  password: z.string().min(0).max(256),
  apiToken: z.string().optional(),
  passphrase: z.string().optional(),
  priority: z.number().int().min(0).max(100).default(0),
  tags: z.array(z.string()).default([]),
});
export type DeviceTypeCredential = z.infer<typeof DeviceTypeCredentialSchema>;

export const DeviceTypeGroupSchema = z.object({
  deviceType: DeviceTypeSchema,
  credentials: z.array(DeviceTypeCredentialSchema).min(1).max(10),
  policy: z.object({
    maxAttempts: z.number().int().min(1).max(10).default(5),
    timeoutMs: z.number().int().min(5000).max(120000).default(30000),
    requireReadOnly: z.boolean().default(true),
  }).default({}),
});
export type DeviceTypeGroup = z.infer<typeof DeviceTypeGroupSchema>;

/** Optional exact-device bindings for customer CPE credentials. These are not used by run-diagnostics. */
export const ExactDeviceCredentialSchema = z.object({
  deviceType: DeviceTypeSchema,
  deviceId: z.string().min(1).max(128),
  host: z.string().min(1).max(253),
  credential: DeviceTypeCredentialSchema,
});
export type ExactDeviceCredential = z.infer<typeof ExactDeviceCredentialSchema>;

export const CredentialStoreSchema = z.object({
  version: z.literal(1),
  deviceTypes: z.array(DeviceTypeGroupSchema),
  // Optional for backwards compatibility with existing encrypted stores.
  exactDeviceCredentials: z.array(ExactDeviceCredentialSchema).max(10000).default([]),
});
export type CredentialStore = z.infer<typeof CredentialStoreSchema>;
