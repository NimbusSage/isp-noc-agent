import { z } from 'zod/v3';

export const CommandCategorySchema = z.enum([
  'system',      // CPU, memory, disk, uptime
  'interfaces',  // Interface status, traffic, errors
  'network',     // Routes, ARP, DNS, DHCP
  'firewall',    // Firewall rules, connections
  'wireless',    // Radio status, stations, signal
  'diagnostic',  // Ping, traceroute, packet sniffer
]);
export type CommandCategory = z.infer<typeof CommandCategorySchema>;

/**
 * Per-parameter schema. Strings MUST carry a `pattern`, `enum` or `check`
 * (a string parameter with none of them is rejected, fail closed); numbers
 * MUST carry `min` and `max`. See commands/params.ts for enforcement.
 */
export interface ParamSpec {
  type: 'string' | 'number' | 'boolean';
  required: boolean;
  default?: unknown;
  /** string: anchored regex describing the full allowed value. */
  pattern?: RegExp;
  /** string: closed set of allowed values. */
  enum?: readonly string[];
  /** string: custom validator returning an error message or null. */
  check?: (value: string) => string | null;
  /** string: maximum length (default 128). */
  maxLength?: number;
  /** number: inclusive bounds (required for numbers). */
  min?: number;
  max?: number;
  /** number: require an integer. */
  integer?: boolean;
}

export interface DeviceCommand {
  name: string;
  category: CommandCategory;
  description: string;
  template: string; // Command template with {{param}} placeholders
  params: Record<string, ParamSpec>;
  readOnly: true; // Always true in this version
}

export interface CommandResult {
  device: string;
  command: string;
  category: string;
  status: 'success' | 'error' | 'timeout' | 'auth_failed';
  result: unknown;
  warnings: string[];
  redactions: number;
  timestamp: string;
  durationMs: number;
}

export interface DeviceClient {
  connect(): Promise<void>;
  execute(command: DeviceCommand, params: Record<string, unknown>): Promise<CommandResult>;
  disconnect(): Promise<void>;
}
