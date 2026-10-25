import { z } from 'zod/v3';

export const LIBRENMS_UNCONFIGURED_URL = 'http://librenms.example.invalid';

export const ServerConfigSchema = z.object({
  credentialStore: z.object({
    path: z.string().default('./config/credentials.enc'),
    passphraseEnvVar: z.string().default('NOC_MCP_PASSPHRASE'),
  }).default({}),
  auditLog: z.object({
    path: z.string().default('./logs/audit.log'),
    maxFileSizeMb: z.number().default(10),
    maxFiles: z.number().default(5),
  }).default({}),
  limits: z.object({
    maxConcurrentDevices: z.number().int().min(1).max(10).default(5),
    commandTimeoutMs: z.number().int().min(5000).max(120000).default(30000),
    maxOutputBytes: z.number().int().min(1024).max(1048576).default(65536),
  }).default({}),
  redaction: z.object({
    enabled: z.boolean().default(true),
    preserveInternalIps: z.array(z.string()).default([]),
    preserveHostnames: z.array(z.string()).default([]),
  }).default({}),
  inventory: z.object({
    enabled: z.boolean().default(true),
    // No shipped host: LIBRENMS_URL (same env var the skills/doctor use) or the
    // inventory.libreNmsBaseUrl override. The .invalid placeholder never
    // resolves, so an unconfigured install fails closed instead of calling out.
    libreNmsBaseUrl: z.string().url().default(() => process.env.LIBRENMS_URL?.trim() || LIBRENMS_UNCONFIGURED_URL),
    libreNmsTokenEnvVar: z.string().min(1).default('LIBRENMS_TOKEN'),
    lookupTimeoutMs: z.number().int().min(1000).max(30000).default(10000),
    staleAfterSeconds: z.number().int().min(60).max(86400).default(900),
  }).default({}),
});
export type ServerConfig = z.infer<typeof ServerConfigSchema>;
