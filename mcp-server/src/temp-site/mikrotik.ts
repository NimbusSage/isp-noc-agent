import { z } from 'zod/v3';
import { planTempOspf } from './ospf.js';
import { checkHostAllowlist } from './allowlist.js';
import {
  DeviceIdSchema, IdentSchema, IncidentIdSchema, INCIDENT_ID_RE, IPV4_CIDR_RE, Ipv4Schema, MtModeSchema,
  Slash30Schema, SsidSchema, WlanIfaceSchema, IDENT_RE, MT_MODE_RE, SSID_RE,
} from '../security/validators.js';

/**
 * Gated RouterOS executor for the Temp OSPF /30 adjacency. Reuses the
 * read-only temp-ospf-plan validation; execution runs the exact planned
 * commands over one persistent SSH session (type-wide credentials are never
 * used — an exact-device mikrotik-ssh binding is required), verifies with
 * print queries, and rolls back by incident tag on any miss.
 */

export const TempOspfApplySchema = z.object({
  incidentId: IncidentIdSchema,
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
  address: Slash30Schema,
  network: Slash30Schema,
  interfaze: IdentSchema,
  ospfArea: IdentSchema.default('backbone-v2'),
  // No default (INC-A-01): ptp on a bridge segment stalls at Exchange;
  // broadcast on a standalone ether breaks ptp expectations. The caller
  // states the type from the live `/interface bridge port print` check.
  ospfType: z.enum(['ptp', 'broadcast'], {
    errorMap: () => ({ message: 'ospfType is required: ptp (standalone ether) or broadcast (bridge/bridge-port member) — determine from /interface bridge port print' }),
  }),
  // Cost rule: ALWAYS 1000 within one fiber drain network, 10000 across
  // different drain networks. Schema-enforced (the cost-lock review: a mesh
  // cost of 10 slipped through the old 1-65535 range). The caller (skill)
  // determines same/cross from topology; omit to take the 1000 default.
  cost: z.number().int().refine((v) => v === 1000 || v === 10000, {
    message: 'Temp OSPF cost must be 1000 (same fiber drain) or 10000 (cross-drain networks); no other value is permitted',
  }).default(1000),
  routerOsVersion: z.enum(['ros6', 'ros7']).default('ros7'),
  // Copied from the router's existing PTP interface-templates (house pattern):
  helloInterval: z.number().int().min(1).max(60).default(1),
  deadInterval: z.number().int().min(2).max(300).default(20),
  priority: z.number().int().min(0).max(255).default(1),
  authId: z.number().int().min(0).max(255).default(1),
  execute: z.boolean(),
});
export type TempOspfApply = z.infer<typeof TempOspfApplySchema>;

export type RouterRun = (cmd: string) => Promise<string>;

export function checkRouterGates(host: string): { ok: true } | { ok: false; error: string } {
  if (process.env.TEMP_SITE_WRITE_ENABLED !== 'true') {
    return { ok: false, error: 'Writes disabled; require TEMP_SITE_WRITE_ENABLED=true' };
  }
  if (process.env.TEMP_SITE_ROUTEROS_TRANSPORT !== 'verified') {
    return { ok: false, error: 'RouterOS transport not verified; confirm exact command behavior on a lab/spare router first' };
  }
  return checkHostAllowlist(host, process.env.TEMP_SITE_ALLOWED_HOSTS, 'TEMP_SITE_ALLOWED_HOSTS');
}

export interface OspfApplyResult {
  applied: boolean;
  rolledBack: boolean;
  tag: string;
  detail: string;
}

export async function applyOspfAdjacency(run: RouterRun, input: TempOspfApply): Promise<OspfApplyResult> {
  const plan = planTempOspf(input);
  if (!plan.valid || !plan.apply || !plan.rollback || !plan.tag) {
    throw new Error(`Plan invalid: ${plan.error}`);
  }
  const tag = plan.tag;
  // NOTE (verified live): RouterOS `where address=X/Y` with CIDR
  // does NOT match. All existence checks filter by comment tag and match
  // address/interface/cost client-side. Never trust a CIDR where-filter.
  const pre = await run(`/ip address print where comment="${tag}"`);
  if (pre.includes(input.address)) {
    throw new Error(`A TEMP-tagged entry already mentions ${input.address} on ${input.host}; refusing`);
  }
  for (const cmd of plan.apply) {
    const out = await run(cmd);
    if (/failure|error|invalid|unknown|bad|already/i.test(out)) {
      throw new Error(`Router rejected command (${out.trim().slice(0, 160)}); no further steps attempted`);
    }
  }
  const verifyAddr = await run(`/ip address print where comment="${tag}"`);
  const verifyOspf = input.routerOsVersion === 'ros7'
    ? await run(`/routing ospf interface-template print where comment="${tag}"`)
    : await run(`/routing ospf interface print where comment="${tag}"`);
  const addrOk = verifyAddr.includes(input.address) && verifyAddr.includes(input.interfaze);
  const ospfOk = verifyOspf.includes(String(input.cost)) && verifyOspf.includes(input.network)
    && (input.routerOsVersion !== 'ros7' || verifyOspf.includes(`type=${input.ospfType}`));
  if (addrOk && ospfOk) {
    return { applied: true, rolledBack: false, tag, detail: 'Address + OSPF adjacency present with expected cost' };
  }
  for (const cmd of plan.rollback) {
    await run(cmd).catch(() => { /* best-effort rollback step */ });
  }
  return { applied: false, rolledBack: true, tag, detail: 'Post-apply verification failed; rollback commands issued (verify removal manually)' };
}

const TAG_RE = /^TEMP-[A-Za-z0-9_.-]{1,64}-REMOVE-WHEN-REPAIRED$/;

export async function rollbackOspfAdjacency(run: RouterRun, tag: string, routerOsVersion: 'ros6' | 'ros7'): Promise<OspfApplyResult> {
  // The tag is interpolated into RouterOS commands: only the exact shape that
  // buildTempOspfCommands() produces from a validated incident ID is accepted.
  if (!TAG_RE.test(tag)) throw new Error('Invalid rollback tag: expected TEMP-<incident-id>-REMOVE-WHEN-REPAIRED with a valid incident ID');
  const removals = [
    `/ip address remove [find comment="${tag}"]`,
    routerOsVersion === 'ros7'
      ? `/routing ospf interface-template remove [find comment="${tag}"]`
      : `/routing ospf interface remove [find comment="${tag}"]`,
  ];
  for (const cmd of removals) {
    await run(cmd).catch(() => { /* best-effort */ });
  }
  const check = await run(`/ip address print where comment="${tag}"`);
  const gone = !check.includes(tag);
  return { applied: false, rolledBack: gone, tag, detail: gone ? 'Temporary objects removed' : 'Removal uncertain; inspect manually' };
}

/* MikroTik wireless station flip (2.4/5GHz sectors and omnis).
 * Minimal diff: only mode + ssid change; frequency/band/security untouched.
 * Rollback restores the recorded previous mode + ssid (no secret handling:
 * security profiles are never read or written). station-bridge is the
 * operator-confirmed mode for bridged temp links. */

export const TempMtFlipSchema = z.object({
  incidentId: IncidentIdSchema,
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
  iface: WlanIfaceSchema.default('wlan1'),
  ssid: SsidSchema,
  execute: z.boolean(),
  confirmWithinSeconds: z.number().int().min(60).max(1800).default(600),
});
export type TempMtFlip = z.infer<typeof TempMtFlipSchema>;

export interface MtFlipResult {
  applied: boolean;
  rolledBack: boolean;
  previous: Record<string, string> | null;
  detail: string;
}

/* Full station recipe (verified live): a mode+ssid flip
 * alone does NOT associate. Station needs frequency=auto,
 * wireless-protocol=any, and rate-set=default alongside station-bridge. */
/* PTP/PTMP PROTOCOL LOCK (operator rule): the station recipe pins
 * wireless-protocol=any (join whatever the AP speaks) and NEVER selects a
 * specific PTP/PTMP protocol mode. Changing protocol modes (e.g. forcing
 * nstreme/nv2, PTP-only) happens only when the specific link requires it —
 * e.g. joining a backhaul radio to a customer AP — as a separate
 * operator-reviewed step with its own readback, never inside a flip. */
const MT_STATION_RECIPE = [
  'mode=station-bridge',
  'frequency=auto',
  'wireless-protocol=any',
  'rate-set=default',
] as const;
const MT_TRACKED_FIELDS = ['mode', 'ssid', 'frequency', 'wireless-protocol', 'rate-set'] as const;

function parseMtField(print: string, field: string): string | null {
  const m = print.match(new RegExp(`${field}=("[^"]*"|\\S+)`));
  if (!m) return null;
  return m[1].replace(/^"|"$/g, '');
}

export function checkMtGates(host: string): { ok: true } | { ok: false; error: string } {
  if (process.env.TEMP_SITE_WRITE_ENABLED !== 'true') {
    return { ok: false, error: 'Writes disabled; require TEMP_SITE_WRITE_ENABLED=true' };
  }
  if (process.env.TEMP_SITE_MT_TRANSPORT !== 'verified') {
    return { ok: false, error: 'MikroTik wireless transport not verified; prove the flip on a customer-free pair first' };
  }
  return checkHostAllowlist(host, process.env.TEMP_SITE_ALLOWED_HOSTS, 'TEMP_SITE_ALLOWED_HOSTS');
}

/* ROS7 snapshot reads (verified live on 7.13.5): the terse
 * `/interface wireless print where` omits advanced fields (rate-set, ...),
 * so per-field `get` over the same persistent session is the snapshot
 * source. Empty read fails closed. NOTE: bare `get` prints nothing over
 * non-interactive SSH exec — the `:put [get ...]` wrapper forces output. */
async function readMtFields(run: RouterRun, host: string, ifaceQ: string): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  for (const f of MT_TRACKED_FIELDS) {
    const raw = (await run(`:put [/interface wireless get ${ifaceQ} ${f}]`)).replace(/[\r\n]+/g, ' ').trim().replace(/^"|"$/g, '');
    if (!raw) throw new Error(`Could not read current ${f} on ${host}; refusing`);
    out[f] = raw;
  }
  return out;
}

/* Values read back from a device are replayed into the rollback command, so
 * they get the same positive allowlist as caller input. A radio whose current
 * mode/SSID contains anything else cannot be restored safely: refuse BEFORE
 * writing rather than risk a malformed/injectable rollback line. */
function assertRestorableSnapshot(fields: Record<string, string>, host: string): void {
  const ok = MT_MODE_RE.test(fields.mode ?? '')
    && SSID_RE.test(fields.ssid ?? '')
    && IDENT_RE.test(fields.frequency ?? '')
    && IDENT_RE.test(fields['wireless-protocol'] ?? '')
    && IDENT_RE.test(fields['rate-set'] ?? '');
  if (!ok) throw new Error(`Current wireless settings on ${host} contain characters this executor cannot restore safely; refusing (use the operator-led path)`);
}

export async function applyMtFlip(run: RouterRun, input: TempMtFlip): Promise<MtFlipResult> {
  const parsed = TempMtFlipSchema.safeParse(input);
  if (!parsed.success) throw new Error(`Validation error: ${parsed.error.issues.map((i) => i.message).join(', ')}`);
  const v = parsed.data;
  const ifaceQ = v.iface;
  const prev = await readMtFields(run, v.host, ifaceQ);
  assertRestorableSnapshot(prev, v.host);
  const wantSsid = v.ssid;
  if (prev.mode === 'station-bridge' && prev.ssid === wantSsid && prev.frequency === 'auto' &&
      prev['wireless-protocol'] === 'any' && prev['rate-set'] === 'default') {
    return { applied: false, rolledBack: false, previous: prev, detail: 'Already in the full requested station state; no write attempted' };
  }
  await run(`/interface wireless set ${ifaceQ} mode=station-bridge ssid="${wantSsid}" frequency=auto wireless-protocol=any rate-set=default`);
  const after = await readMtFields(run, v.host, ifaceQ);
  const checks: Record<string, string> = {
    mode: 'station-bridge', ssid: wantSsid, frequency: 'auto', 'wireless-protocol': 'any', 'rate-set': 'default',
  };
  const bad = Object.entries(checks).filter(([f, want]) => after[f] !== want).map(([f]) => f);
  if (bad.length) {
    await run(`/interface wireless set ${ifaceQ} mode="${prev.mode}" ssid="${prev.ssid}" frequency="${prev.frequency}" wireless-protocol="${prev['wireless-protocol']}" rate-set="${prev['rate-set']}"`).catch(() => { /* best effort */ });
    return { applied: false, rolledBack: true, previous: prev, detail: `Readback mismatch on ${bad.join(',')}; restore attempted (verify manually)` };
  }
  return { applied: true, rolledBack: false, previous: prev, detail: 'Full station recipe applied (mode/ssid/freq-auto/proto-any/rates-default)' };
}

/** Restore target for rollbackMtFlip; every field is a closed character set. */
export const MtRestoreSchema = z.object({
  iface: WlanIfaceSchema.default('wlan1'),
  mode: MtModeSchema,
  ssid: SsidSchema,
  frequency: IdentSchema.optional(),
  protocol: IdentSchema.optional(),
  rateset: IdentSchema.optional(),
});

export const TempMtRollbackSchema = MtRestoreSchema.extend({
  incidentId: IncidentIdSchema.optional(),
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
});

export async function rollbackMtFlip(run: RouterRun, input: { host: string; iface?: string; mode: string; ssid: string; frequency?: string; protocol?: string; rateset?: string }): Promise<MtFlipResult> {
  const parsed = MtRestoreSchema.safeParse({
    iface: input.iface || undefined,
    mode: input.mode,
    ssid: input.ssid,
    frequency: input.frequency || undefined,
    protocol: input.protocol || undefined,
    rateset: input.rateset || undefined,
  });
  if (!parsed.success) throw new Error(`Invalid rollback parameters: ${parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join(', ')}`);
  const v = parsed.data;
  const sets = [`mode="${v.mode}"`, `ssid="${v.ssid}"`];
  if (v.frequency) sets.push(`frequency="${v.frequency}"`);
  if (v.protocol) sets.push(`wireless-protocol="${v.protocol}"`);
  if (v.rateset) sets.push(`rate-set="${v.rateset}"`);
  await run(`/interface wireless set ${v.iface} ${sets.join(' ')}`);
  const after = await run(`/interface wireless print where name=${v.iface}`);
  const ok = parseMtField(after, 'mode') === v.mode && parseMtField(after, 'ssid') === v.ssid;
  return { applied: false, rolledBack: ok, previous: null, detail: ok ? 'Restored previous wireless state' : 'Restore uncertain; verify manually' };
}

/* AP-side WDS toggle for station-bridge data path (verified live): a
 * station-bridge client associates fine against a wds-disabled AP but
 * bridged traffic stalls. Enabling wds-mode=dynamic on
 * the AP restores flow; the station reassociates automatically. Snapshot and
 * restore the previous value; never leave it changed after the temp link. */
export const TempApWdsSchema = z.object({
  incidentId: IncidentIdSchema,
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
  iface: WlanIfaceSchema.default('wlan1'),
  wdsMode: z.enum(['dynamic', 'disabled']).default('dynamic'),
  execute: z.boolean(),
});
export type TempApWds = z.infer<typeof TempApWdsSchema>;

export async function applyApWds(run: RouterRun, input: TempApWds): Promise<{ applied: boolean; previous: string | null; detail: string }> {
  const parsed = TempApWdsSchema.safeParse(input);
  if (!parsed.success) throw new Error(`Validation error: ${parsed.error.issues.map((i) => i.message).join(', ')}`);
  const v = parsed.data;
  const ifaceQ = v.iface;
  const before = await run(`/interface wireless print where name=${ifaceQ}`);
  const prev = parseMtField(before, 'wds-mode');
  if (prev === null) throw new Error(`Could not read current wds-mode on ${v.host}; refusing`);
  if (prev === v.wdsMode) return { applied: false, previous: prev, detail: 'Already in requested wds-mode; no write attempted' };
  await run(`/interface wireless set ${ifaceQ} wds-mode=${v.wdsMode}`);
  const after = await run(`/interface wireless print where name=${ifaceQ}`);
  if (parseMtField(after, 'wds-mode') !== v.wdsMode) {
    // prev came from the device: only restore a value that is a plain identifier.
    if (IDENT_RE.test(prev)) await run(`/interface wireless set ${ifaceQ} wds-mode=${prev}`).catch(() => { /* best effort */ });
    return { applied: false, previous: prev, detail: 'Readback mismatch; restore attempted (verify manually)' };
  }
  return { applied: true, previous: prev, detail: `wds-mode set to ${v.wdsMode} (was ${prev})` };
}

/* Station re-associate bounce (verified need): AP-side changes
 * (e.g. enabling WDS) only take effect for station-bridge clients that
 * associate AFTER the change. Bouncing the station interface forces a fresh
 * association with current AP capabilities. No config is altered. */
export const TempMtBounceSchema = z.object({
  incidentId: IncidentIdSchema,
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
  iface: WlanIfaceSchema.default('wlan1'),
  execute: z.boolean(),
});
export type TempMtBounce = z.infer<typeof TempMtBounceSchema>;

export async function applyMtBounce(run: RouterRun, input: TempMtBounce): Promise<{ bounced: boolean; detail: string }> {
  const parsed = TempMtBounceSchema.safeParse(input);
  if (!parsed.success) throw new Error(`Validation error: ${parsed.error.issues.map((i) => i.message).join(', ')}`);
  const v = parsed.data;
  const ifaceQ = v.iface;
  const before = await run(`/interface wireless print where name=${ifaceQ}`);
  if (!before.includes('mode=station-bridge')) {
    throw new Error(`Interface ${ifaceQ} is not a station-bridge client; bounce is only for temp station links`);
  }
  await run(`/interface wireless disable ${ifaceQ}`);
  await new Promise((r) => setTimeout(r, 5000));
  await run(`/interface wireless enable ${ifaceQ}`);
  await new Promise((r) => setTimeout(r, 15000));
  const after = await run(`/interface wireless monitor ${ifaceQ} once`);
  const up = /status:\s*connected-to-ess/.test(after);
  return { bounced: up, detail: up ? 'Re-associated after bounce' : 'Not associated after bounce; observe log, do not flap repeatedly' };
}

/* Same-subnet temp alias on the donor station (verified need,
 * INC-G-01): Phase-2 island diagnosis needs a donor-bridge address in
 * the island subnet for forced-L2 ping and SSH hops to island devices.
 * Plain address add/remove by incident tag; same gates as the flip.
 * Removed with temp-mt-alias-remove (or the incident undo) — never left. */
export const TempMtAliasSchema = z.object({
  incidentId: IncidentIdSchema,
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
  address: z.string().regex(IPV4_CIDR_RE, 'Canonical IPv4/CIDR (no leading zeros), e.g. 10.0.20.60/24')
    .refine((a) => { const p = Number(a.split('/')[1]); return p >= 1 && p <= 32; }, { message: 'CIDR prefix must be 1-32' }),
  interfaze: IdentSchema.default('bridge1'),
  execute: z.boolean(),
});
export type TempMtAlias = z.infer<typeof TempMtAliasSchema>;

export interface MtAliasResult {
  applied: boolean;
  rolledBack: boolean;
  removed: boolean;
  detail: string;
}

export function aliasTag(incidentId: string): string {
  return `TEMP-${incidentId}-REMOVE-WHEN-REPAIRED`;
}

export async function applyMtAlias(run: RouterRun, input: TempMtAlias): Promise<MtAliasResult> {
  const parsed = TempMtAliasSchema.safeParse(input);
  if (!parsed.success) throw new Error(`Validation error: ${parsed.error.issues.map((i) => i.message).join(', ')}`);
  const v = parsed.data;
  const tag = aliasTag(v.incidentId);
  const before = await run('/ip address print');
  if (before.includes(v.address)) {
    return { applied: false, rolledBack: false, removed: false, detail: 'Address already present; no write attempted' };
  }
  await run(`/ip address add address=${v.address} interface="${v.interfaze}" comment="${tag}"`);
  const after = await run('/ip address print');
  if (after.includes(v.address) && after.includes(tag)) {
    return { applied: true, rolledBack: false, removed: false, detail: `Temp alias ${v.address} on ${v.interfaze} (tagged ${tag})` };
  }
  await run(`/ip address remove [find comment="${tag}"]`).catch(() => { /* best effort */ });
  return { applied: false, rolledBack: true, removed: false, detail: 'Readback mismatch; tag removal attempted (verify manually)' };
}

export async function removeMtAlias(run: RouterRun, input: { host: string; incidentId: string }): Promise<MtAliasResult> {
  if (!INCIDENT_ID_RE.test(input.incidentId)) throw new Error('Invalid incident ID');
  const tag = aliasTag(input.incidentId);
  await run(`/ip address remove [find comment="${tag}"]`);
  const after = await run('/ip address print');
  const gone = !after.includes(tag);
  return { applied: false, rolledBack: false, removed: gone, detail: gone ? `Tag ${tag} removed` : 'Tag still present; verify manually' };
}
