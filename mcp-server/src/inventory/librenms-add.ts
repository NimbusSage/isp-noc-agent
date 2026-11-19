import { z } from 'zod/v3';
import { Ipv4Schema, ReasonSchema } from '../security/validators.js';
import { assertSafeBaseUrl, readResponseBodyLimited } from './librenms.js';

/**
 * Add ONE device to LibreNMS.
 *
 * This is inventory, not a device: it never touches the router/radio and needs no
 * device credential. What it does need is proof that the device answers SNMP,
 * because LibreNMS's own add path happily creates a device that never polls —
 * a down entry somebody has to find and delete later. So the caller probes SNMP
 * first (inventory/snmp-probe.ts) and refuses to add on silence.
 *
 * Two operator rules are encoded here, not left to the caller:
 *
 *  - NO FORCE. `force_add` is sent as false and there is no parameter to change
 *    that. A sysName collision is real information — two devices claiming one
 *    name — and forcing past it is how an inventory ends up with entries the
 *    poller cannot tell apart. LibreNMS's refusal is surfaced verbatim.
 *  - The community comes from the environment (NOC_SNMP_COMMUNITY), never from a
 *    tool argument, and it is redacted from every message this module returns.
 */

export const LibrenmsAddSchema = z.object({
  host: Ipv4Schema,
  reason: ReasonSchema,
  execute: z.boolean().default(false),
});
export type LibrenmsAdd = z.infer<typeof LibrenmsAddSchema>;

export interface LibrenmsAddRequest {
  baseUrl: string;
  token: string;
  timeoutMs: number;
}

export interface LibrenmsAddOutcome {
  status: 'added' | 'error';
  /** LibreNMS's own words, bounded. Never contains the community (caller still redacts). */
  message: string;
  deviceId: string | null;
}

/** The exact request this module will send, for the dry-run plan. Nothing secret. */
export function librenmsAddRequestPreview(host: string): Record<string, unknown> {
  return {
    hostname: host,
    transport: 'udp',
    port: 161,
    version: 'v2c',
    community: '<from NOC_SNMP_COMMUNITY>',
    force_add: false,
  };
}

export async function postLibrenmsDevice(
  host: string,
  community: string,
  request: LibrenmsAddRequest,
): Promise<LibrenmsAddOutcome> {
  const baseUrl = assertSafeBaseUrl(request.baseUrl);
  if (!request.token.trim()) throw new Error('LibreNMS API token is not configured');
  if (baseUrl.hostname.toLowerCase().endsWith('.invalid')) {
    throw new Error('LibreNMS base URL is not configured (set LIBRENMS_URL or inventory.libreNmsBaseUrl)');
  }

  const body = {
    hostname: host,
    transport: 'udp',
    port: 161,
    version: 'v2c',
    community,
    // Never true. See the module comment: a sysName collision is a signal, not
    // an obstacle.
    force_add: false,
  };

  const response = await fetch(new URL('/api/v0/devices', baseUrl), {
    method: 'POST',
    headers: {
      'X-Auth-Token': request.token,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(request.timeoutMs),
    redirect: 'error',
  });
  const text = await readResponseBodyLimited(response);

  let parsed: Record<string, unknown> | null = null;
  try {
    const candidate = JSON.parse(text) as unknown;
    if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) parsed = candidate as Record<string, unknown>;
  } catch {
    // Not JSON: keep LibreNMS's raw text (it is the only explanation available).
  }

  const status = typeof parsed?.status === 'string' ? parsed.status : null;
  const message = typeof parsed?.message === 'string'
    ? parsed.message
    : parsed?.errors
      ? JSON.stringify(parsed.errors).slice(0, 400)
      : text.trim().slice(0, 400) || `HTTP ${response.status}`;
  const devices = Array.isArray(parsed?.devices) ? (parsed?.devices as Record<string, unknown>[]) : [];
  const deviceId = devices[0]?.device_id !== undefined && devices[0]?.device_id !== null ? String(devices[0].device_id) : null;

  const ok = response.ok && status === 'ok';
  return { status: ok ? 'added' : 'error', message: message.replace(/\s+/g, ' ').slice(0, 400), deviceId: ok ? deviceId : null };
}
