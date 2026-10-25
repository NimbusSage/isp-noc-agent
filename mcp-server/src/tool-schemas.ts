/**
 * Single source of truth for every tool's INPUT schema.
 *
 * Both transports use this table: index.ts registers each shape with the MCP
 * SDK, and NocMcpServer.handleToolCall() parses with the very same shapes, so
 * tool-cli.ts (which calls handleToolCall directly) can no longer skip
 * validation that the MCP transport enforces.
 *
 * P0 history: routeros-write was registered in server.ts but missing from the
 * table that used to live in index.ts, so the MCP server threw
 * "Missing MCP input schema for tool: routeros-write" at startup. The
 * registration code (register-tools.ts) still fails fast on a missing entry,
 * and tests/tool-schemas.test.ts asserts coverage so it cannot regress.
 */
import { z } from 'zod/v3';
import { DeviceTypeSchema } from './credentials/schema.js';
import { RouterOsWriteSchema } from './writes/routeros-writes.js';
import {
  DeviceIdSchema,
  HostArgSchema,
  IdentSchema,
  IncidentIdSchema,
  Ipv4Schema,
  MtModeSchema,
  Slash30Schema,
  SsidSchema,
  WlanIfaceSchema,
  IPV4_CIDR_RE,
} from './security/validators.js';

export type ToolShape = Record<string, z.ZodTypeAny>;

const CostSchema = z.number().int().refine((v) => v === 1000 || v === 10000, {
  message: 'Temp OSPF cost must be 1000 (same fiber drain) or 10000 (cross-drain networks)',
});

const PskSourceShape = z.object({
  type: z.enum(['ap-config', 'env']),
  apHost: Ipv4Schema.optional(),
  apDeviceId: DeviceIdSchema.optional(),
  index: z.number().int().min(0).optional(),
});

const ConfirmSeconds = z.number().int().min(60).max(1800);

const TempRadioShape: ToolShape = {
  op: z.string().min(1).max(32),
  incidentId: IncidentIdSchema,
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
  mode: z.string().max(16).optional(),
  ssid: SsidSchema.optional(),
  frequencyMhz: z.number().int().optional(),
  chanbw: z.number().int().optional(),
  previousFrequencyMhz: z.number().int().optional(),
  confirmWithinSeconds: ConfirmSeconds.optional(),
  pskSource: PskSourceShape.optional(),
};

export const runDiagnosticsShape: ToolShape = {
  host: HostArgSchema,
  // Shared enum — stays in sync with credentials/schema.ts automatically.
  deviceType: DeviceTypeSchema,
  command: z.string().min(1, 'Command name is required').max(64),
  params: z.record(z.unknown()).default({}),
};

export const runExactDeviceDiagnosticsShape: ToolShape = {
  deviceId: DeviceIdSchema,
  host: HostArgSchema,
  deviceType: z.literal('ubnt-airos'),
  command: z.string().min(1, 'Command name is required').max(64),
  params: z.record(z.unknown()).default({}),
};

export const toolSchemas: Record<string, ToolShape> = {
  'run-diagnostics': runDiagnosticsShape,
  'run-exact-device-diagnostics': runExactDeviceDiagnosticsShape,
  'lookup-device-by-exact-ip': {
    ip: Ipv4Schema,
  },
  'list-device-types': {},
  'routeros-write': { ...RouterOsWriteSchema.shape },
  'temp-radio-plan': { ...TempRadioShape },
  'temp-radio-apply': {
    ...TempRadioShape,
    // Absent = dry-run plan (documented behavior); only an explicit true executes.
    execute: z.boolean().default(false),
  },
  'temp-radio-rollback': {
    incidentId: IncidentIdSchema,
    host: Ipv4Schema,
    deviceId: DeviceIdSchema,
  },
  'temp-ospf-plan': {
    incidentId: IncidentIdSchema,
    host: HostArgSchema,
    interfaze: IdentSchema,
    address: Slash30Schema,
    network: Slash30Schema,
    ospfType: z.enum(['ptp', 'broadcast']),
    ospfArea: IdentSchema.optional(),
    cost: CostSchema.optional(),
    routerOsVersion: z.enum(['ros6', 'ros7']).optional(),
  },
  'temp-ospf-apply': {
    incidentId: IncidentIdSchema,
    host: Ipv4Schema,
    deviceId: DeviceIdSchema,
    address: Slash30Schema,
    network: Slash30Schema,
    interfaze: IdentSchema,
    ospfType: z.enum(['ptp', 'broadcast']),
    ospfArea: IdentSchema.optional(),
    cost: CostSchema.optional(),
    routerOsVersion: z.enum(['ros6', 'ros7']).optional(),
    helloInterval: z.number().int().min(1).max(60).optional(),
    deadInterval: z.number().int().min(2).max(300).optional(),
    priority: z.number().int().min(0).max(255).optional(),
    authId: z.number().int().min(0).max(255).optional(),
    execute: z.boolean().default(false),
  },
  'temp-ospf-rollback': {
    incidentId: IncidentIdSchema,
    host: Ipv4Schema,
    deviceId: DeviceIdSchema,
    routerOsVersion: z.enum(['ros6', 'ros7']).optional(),
  },
  'temp-mt-flip': {
    incidentId: IncidentIdSchema,
    host: Ipv4Schema,
    deviceId: DeviceIdSchema,
    iface: WlanIfaceSchema.optional(),
    ssid: SsidSchema,
    execute: z.boolean().default(false),
    confirmWithinSeconds: ConfirmSeconds.optional(),
  },
  'temp-mt-rollback': {
    incidentId: IncidentIdSchema,
    host: Ipv4Schema,
    deviceId: DeviceIdSchema,
    iface: WlanIfaceSchema.optional(),
    mode: MtModeSchema,
    ssid: SsidSchema,
    frequency: IdentSchema.optional(),
    protocol: IdentSchema.optional(),
    rateset: IdentSchema.optional(),
  },
  'temp-ap-wds': {
    incidentId: IncidentIdSchema,
    host: Ipv4Schema,
    deviceId: DeviceIdSchema,
    iface: WlanIfaceSchema.optional(),
    wdsMode: z.enum(['dynamic', 'disabled']).optional(),
    execute: z.boolean().default(false),
  },
  'temp-mt-bounce': {
    incidentId: IncidentIdSchema,
    host: Ipv4Schema,
    deviceId: DeviceIdSchema,
    iface: WlanIfaceSchema.optional(),
    execute: z.boolean().default(false),
  },
  'temp-mt-alias': {
    incidentId: IncidentIdSchema,
    host: Ipv4Schema,
    deviceId: DeviceIdSchema,
    address: z.string().regex(IPV4_CIDR_RE, 'Canonical IPv4/CIDR (no leading zeros), e.g. 10.0.20.60/24'),
    interfaze: IdentSchema.optional(),
    execute: z.boolean().default(false),
  },
  'temp-mt-alias-remove': {
    incidentId: IncidentIdSchema,
    host: Ipv4Schema,
    deviceId: DeviceIdSchema,
  },
};

export type ParsedToolArgs =
  | { ok: true; data: Record<string, unknown> }
  | { ok: false; error: string };

/**
 * Parse tool arguments exactly like the MCP SDK does (z.object over the
 * registered shape: unknown keys stripped, defaults applied).
 */
export function parseToolArgs(toolName: string, args: Record<string, unknown>): ParsedToolArgs {
  if (!Object.prototype.hasOwnProperty.call(toolSchemas, toolName)) {
    return { ok: false, error: `Unknown tool: ${toolName}` };
  }
  const parsed = z.object(toolSchemas[toolName]).safeParse(args);
  if (!parsed.success) {
    return {
      ok: false,
      error: parsed.error.issues.map((i) => (i.path.length ? `${i.path.join('.')}: ${i.message}` : i.message)).join('; '),
    };
  }
  return { ok: true, data: parsed.data as Record<string, unknown> };
}
