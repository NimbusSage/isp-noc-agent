import { z } from 'zod';
import type { AllowlistedKey, CfgEdit } from './airos-ssh.js';
import { checkHostAllowlist } from './allowlist.js';
import { DeviceIdSchema, IncidentIdSchema, Ipv4Schema, SsidSchema } from '../security/validators.js';

/**
 * Typed UBNT airOS radio operations for temp-site-connection.
 * PLAN path is fully implemented and read-only. EXECUTE path is gated and
 * its SSH transport is stubbed until airOS apply syntax is verified against
 * a real radio with the operator (never guessed). Safe-mode semantics:
 * every op carries a confirm-by deadline; a missed verification reverts.
 */

const IncidentId = IncidentIdSchema;
const HostV4 = Ipv4Schema;
const DeviceId = DeviceIdSchema;

export const PskSourceSchema = z.union([
  z.object({
    type: z.literal('ap-config'),
    apHost: HostV4,
    apDeviceId: DeviceId,
  }),
  z.object({
    type: z.literal('env'),
    index: z.number().int().min(0).default(0),
  }),
]);
export const SetModeOpSchema = z.object({
  op: z.literal('set-mode'),
  incidentId: IncidentId,
  host: HostV4,
  deviceId: DeviceId,
  mode: z.enum(['managed', 'master']),
  ssid: SsidSchema,
  confirmWithinSeconds: z.number().int().min(60).max(1800).default(600),
  // Secured-AP support: without pskSource a managed flip applies an
  // open-profile supplicant (works only for open APs; assoc verify fails
  // closed on WPA2 and safe-mode rolls back). With pskSource the key is
  // resolved at execute time (never planned, never logged).
  pskSource: PskSourceSchema.optional(),
});
export const SetFrequencyOpSchema = z.object({
  op: z.literal('set-frequency'),
  incidentId: IncidentId,
  host: HostV4,
  deviceId: DeviceId,
  frequencyMhz: z.number().int().min(4900).max(5925),
  chanbw: z.union([z.literal(10), z.literal(20), z.literal(30), z.literal(40)]).default(20),
  previousFrequencyMhz: z.number().int().min(0).max(5925),
  confirmWithinSeconds: z.number().int().min(60).max(1800).default(600),
});
export const RebootOpSchema = z.object({
  op: z.literal('reboot'),
  incidentId: IncidentId,
  host: HostV4,
  deviceId: DeviceId,
  confirmWithinSeconds: z.number().int().min(120).max(1800).default(600),
});
export const TempRadioOpSchema = z.discriminatedUnion('op', [SetModeOpSchema, SetFrequencyOpSchema, RebootOpSchema]);
export type TempRadioOp = z.infer<typeof TempRadioOpSchema>;

export interface TempRadioPlan {
  valid: boolean;
  error?: string;
  op?: TempRadioOp;
  tag?: string;
  applySteps?: string[];
  verifySteps?: string[];
  rollbackSteps?: string[];
  confirmBy?: string;
  executes?: false;
}

export function planTempRadio(input: unknown): TempRadioPlan {
  const parsed = TempRadioOpSchema.safeParse(input);
  if (!parsed.success) {
    return { valid: false, error: parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join(', ') };
  }
  const op = parsed.data;
  const tag = `TEMP-${op.incidentId}-REMOVE-WHEN-REPAIRED`;
  const confirmBy = new Date(Date.now() + op.confirmWithinSeconds * 1000).toISOString();
  if (op.op === 'set-mode') {
    return {
      valid: true, op, tag, executes: false, confirmBy,
      applySteps: [
        `Snapshot /tmp/system.cfg from ${op.host} (before-state, operator-held keys redacted in records)`,
        `Set radio.1.mode=${op.mode}; station profile SSID=${op.ssid} (security unchanged, operator-held)`,
        ...(op.mode === 'managed'
          ? [op.pskSource
            ? `Resolve station PSK at execute time (${op.pskSource.type === 'ap-config' ? `AP-config ${op.pskSource.apHost}` : `env index ${op.pskSource.index}`}); audit records source only`
            : `No pskSource: open-profile supplicant only — works for OPEN APs; verify target auth from scan-result flags (e.g. [WPA2-PSK-CCMP]) before executing`]
          : []),
        `Apply with /usr/etc/rc.d/rc.softrestart save (operator-verified airMAX apply; no full reboot needed)`,
      ],
      verifySteps: [
        `Read back radio.1.mode + supplicant profile from ${op.host}`,
        op.mode === 'managed'
          ? `Confirm association to the intended BSSID with stable signal; watch log for frequency-select loop vs 4-way handshake timeout`
          : `Confirm AP role broadcasting ${op.ssid} and expected client re-associations`,
        `Confirm wired-side management path to the reachable site is intact`,
      ],
      rollbackSteps: [
        `Restore exact saved /tmp/system.cfg to ${op.host} and re-apply`,
        `Verify original role/clients recovered before any further change`,
      ],
    };
  }
  if (op.op === 'set-frequency') {
    return {
      valid: true, op, tag, executes: false, confirmBy,
      applySteps: [
        `Snapshot /tmp/system.cfg from ${op.host}`,
        `Set radio.1.freq to ${op.frequencyMhz} MHz @ ${op.chanbw} MHz (previous: ${op.previousFrequencyMhz === 0 ? 'AUTO (misconfiguration — do not restore as AUTO, use documented)' : op.previousFrequencyMhz + ' MHz'})`,
        `Apply via verified airOS method ONLY`,
      ],
      verifySteps: [
        `Read back radio.1.freq from ${op.host}`,
        `Confirm expected associations re-form and link health is at/above baseline`,
      ],
      rollbackSteps: [
        op.previousFrequencyMhz === 0
          ? `Restore documented frequency from MyMate device-name suffix (never AUTO)`
          : `Restore previous frequency ${op.previousFrequencyMhz} MHz from saved config`,
        `Verify associations recover`,
      ],
    };
  }
  return {
    valid: true, op, tag, executes: false, confirmBy,
    applySteps: [`Reboot ${op.host} (full reboot, not wireless-stack-only)`],
    verifySteps: [
      `Allow full boot + multi-minute observation window (low-margin links associate on their own)`,
      `Confirm role, associations, and management reachability from ${op.host}`,
    ],
    rollbackSteps: [`A reboot has no config rollback; if state regressed, restore exact saved /tmp/system.cfg`],
  };
}

/** Execution gate check — transport itself is stubbed pending verified syntax. */
export function checkExecuteGates(): { ok: true } | { ok: false; error: string } {
  if (process.env.TEMP_SITE_WRITE_ENABLED !== 'true') {
    return { ok: false, error: 'Writes disabled; require TEMP_SITE_WRITE_ENABLED=true (runtime kill switch)' };
  }
  return { ok: true };
}

/** Transport gate: airOS SSH may only run after safe-area verification. */
export function checkTransportGates(host: string): { ok: true } | { ok: false; error: string } {
  if (process.env.TEMP_SITE_AIROS_TRANSPORT !== 'verified') {
    return { ok: false, error: 'airOS SSH transport not verified; confirm cfgmtd save flags + reboot behavior in a customer-free test area first' };
  }
  return checkHostAllowlist(host, process.env.TEMP_SITE_ALLOWED_HOSTS, 'TEMP_SITE_ALLOWED_HOSTS', '; writes are scope-allowlisted');
}

/** Map a validated op to exact /tmp/system.cfg edits (keys from allowlist). */
export function opToEdits(op: TempRadioOp, psk?: string): CfgEdit[] {
  if (op.op === 'set-mode') {
    if (op.mode === 'managed') {
      const edits: CfgEdit[] = [
        { key: 'radio.1.mode', value: 'managed' },
        { key: 'wpasupplicant.status', value: 'enabled' },
        { key: 'wpasupplicant.device.1.status', value: 'enabled' },
        { key: 'wpasupplicant.profile.1.network.1.ssid', value: op.ssid },
      ];
      if (psk !== undefined) {
        // Secured AP (verified live: open-profile supplicant scans
        // but never completes against WPA2-PSK). Key mgmt must flip with the key.
        edits.push({ key: 'wpasupplicant.profile.1.network.1.key_mgmt.1.name', value: 'WPA-PSK' });
        edits.push({ key: 'wpasupplicant.profile.1.network.1.psk', value: psk });
      }
      return edits;
    }
    return [
      { key: 'radio.1.mode', value: 'master' },
      { key: 'wpasupplicant.status', value: 'disabled' },
      { key: 'wpasupplicant.device.1.status', value: 'disabled' },
    ];
  }
  if (op.op === 'set-frequency') {
    return [
      { key: 'radio.1.freq', value: String(op.frequencyMhz) },
      { key: 'radio.1.chanbw', value: String(op.chanbw) },
    ];
  }
  return [];
}

/* Station PSK resolution (operator-approved): a managed station
 * joining a WPA2-PSK AP needs the AP's key. Sources, in order:
 *   1. ap-config — read `aaa.1.wpa.psk` from the target AP's own live
 *      /tmp/system.cfg over its exact-device binding (exact key, no guessing).
 *   2. env — TEMP_SITE_WPA_KEYS fallback list (JSON array, or a single key).
 * Values are validated (printable, sed-safe, 8..63 chars) and NEVER logged:
 * every audit/metadata surface records only the SOURCE (`ap-config` or
 * `env:<index>`). Recon must quote key names + lengths, never values. */

export function parseEnvWpaKeys(raw: string | undefined): string[] {
  if (!raw) return [];
  const trimmed = raw.trim();
  if (!trimmed) return [];
  let list: unknown;
  if (trimmed.startsWith('[')) {
    try {
      list = JSON.parse(trimmed);
    } catch {
      throw new Error('TEMP_SITE_WPA_KEYS is not valid JSON; use ["key1","key2"] or a single key');
    }
    if (!Array.isArray(list)) throw new Error('TEMP_SITE_WPA_KEYS JSON must be an array of strings');
  } else {
    list = [trimmed];
  }
  const keys = (list as unknown[]).filter((k): k is string => typeof k === 'string' && k.length > 0);
  if (keys.length !== (list as unknown[]).length) throw new Error('TEMP_SITE_WPA_KEYS must contain only non-empty strings');
  return keys;
}

export function validatePsk(psk: string, origin: string): string {
  if (!/^[\x21-\x7e]{8,63}$/.test(psk)) {
    throw new Error(`${origin}: key must be 8..63 printable non-space ASCII (WPA2-PSK range)`);
  }
  if (/["'`$\\!;|&<>]/.test(psk)) {
    throw new Error(`${origin}: key contains shell/sed metacharacters this transport cannot safely apply; use another key`);
  }
  return psk;
}

export interface PskResolution {
  psk: string;
  source: string;
}

export async function resolveStationPsk(
  opts: { apRead?: () => Promise<string>; apHost?: string; envIndex?: number },
): Promise<PskResolution> {
  // 1. AP-config primary: exact key from the AP we are joining.
  if (opts.apRead) {
    const out = await opts.apRead();
    const m = out.match(/^aaa\.1\.wpa\.psk=(.*)$/m);
    if (m && m[1]) {
      return { psk: validatePsk(m[1].trim(), 'ap-config'), source: 'ap-config' };
    }
    throw new Error(`AP ${opts.apHost ?? 'unknown'} has no aaa.1.wpa.psk (open AP? wrong host?); refusing to guess — supply env fallback or confirm open auth`);
  }
  // 2. Env fallback list.
  const keys = parseEnvWpaKeys(process.env.TEMP_SITE_WPA_KEYS);
  if (!keys.length) {
    throw new Error('No PSK source: AP lookup not requested and TEMP_SITE_WPA_KEYS is unset/empty');
  }
  const idx = opts.envIndex ?? 0;
  if (idx < 0 || idx >= keys.length) {
    throw new Error(`envIndex ${idx} out of range for TEMP_SITE_WPA_KEYS (${keys.length} keys)`);
  }
  return { psk: validatePsk(keys[idx], `env:${idx}`), source: `env:${idx}` };
}

/** Strip any captured key material from user-facing text. Audit/metadata must
 * call this on readbacks that may include `*.psk=` lines. Key NAMES stay. */
export function redactCfgValues(text: string): string {
  return text.replace(/^([A-Za-z0-9._-]*psk=).*$/gim, '$1<redacted>');
}
