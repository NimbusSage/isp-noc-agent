import { z } from 'zod/v3';
import { checkWriteHost, parseIPv4 } from '../security/hosts.js';
import { DeviceIdSchema, Ipv4Schema, ReasonSchema } from '../security/validators.js';
import type { CfgEdit } from '../temp-site/airos-ssh.js';
import type { DeviceType } from '../credentials/schema.js';

/**
 * Gated SNMP provisioning — the step between "the device answers ping" and
 * "LibreNMS can poll it". Devices that are up but unmonitored almost always
 * have SNMP switched off, which only a login can change; this executor does
 * that login with the credentials the MCP connector already holds, under the
 * same model as `routeros-write` / `routeros-temp-access`:
 *
 *   1. NOC_WRITE_ENABLED=true                — master kill switch (default OFF)
 *   2. NOC_WRITE_SNMP_TRANSPORT=verified     — operator validated these exact
 *      commands on one spare device of each vendor (its own acknowledgement:
 *      `/snmp set` and the airOS `snmp.*` keys are separate transports)
 *   3. write scope                           — private/link-local/loopback IPv4
 *      by default; NOC_WRITE_ALLOWED_HOSTS, when set, replaces that default
 *   4. execute=true                          — dry-run plan by default
 *   5. credential                            — a unique exact-device binding, or
 *      (only with NOC_WRITE_SNMP_POOL_CREDENTIALS=true) the device-type pool the
 *      read path already uses, tried in priority order and bounded. Type-wide
 *      credentials are a deliberate exception here and nowhere else: the whole
 *      point is devices nobody provisioned yet.
 *   6. closed op set                         — snmp-enable | snmp-verify
 *   7. read-back verification                — every apply ends with a print/grep
 *
 * The community is a secret with a small blast radius, so it never travels in
 * the tool call or on our command line: it is read from NOC_SNMP_COMMUNITY
 * (environment or .env, like every other credential) and redacted from every
 * plan, result, error and audit line.
 *
 * MikroTik installs the ORGANIZATION'S STANDARD config, never a minimal one:
 * one read-only community with `security=none`, the LibreNMS poller sources in
 * `addresses=` (NOC_SNMP_COMMUNITY_ADDRESSES — the list of networks allowed to
 * poll, org data, so it is configuration and not a literal in this file), and
 * `trap-community` pointed at the same community. `addresses=` is applied only
 * when that list is configured: an address list is exactly what silently breaks
 * polling when it is wrong, so the list has to be a deliberate, reviewable value.
 *
 * The one setting that looks unrelated but is the most common cause of a device
 * that "has SNMP on" and still does not answer: `/snmp set src-address`. When it
 * holds an address the device no longer has (a stale management IP after a
 * renumber), the daemon stops answering queries entirely. `::` means "any local
 * address", so a configured value is reset to `::` before anything else.
 *
 * Supported ops (closed set):
 *   snmp-enable  mikrotik: standard community (add or set), enable SNMP, repair
 *                a stale src-address.
 *                aios:      set `snmp.status=enabled` + `snmp.community`, applied
 *                through the snapshot/rollback airOS cfg transport.
 *                wave:      set `services.snmp.community` + `services.snmp.enabled`
 *                through `ucfg edit` + `ucfg commit` (WaveOS applies edits live).
 *   snmp-verify  read the device's SNMP settings back; never writes, so it needs
 *                no kill switch and no community — useful for auditing a batch.
 *
 * Writes are idempotent: re-running is safe, and other vendors' communities are
 * never touched.
 */

export const SNMP_OPS = ['snmp-enable', 'snmp-verify'] as const;
export const SNMP_VENDORS = ['mikrotik', 'airos', 'wave'] as const;

/**
 * sysContact is an org-wide value, not a per-device decision, so it is read from
 * `NOC_SNMP_CONTACT` and never from a literal here: the name identifies the
 * organization, and tracked files stay org-agnostic (the estate value is
 * configuration — `.env` on this install, the site pack for the team). Unset
 * means sysContact is left exactly as it is on the device.
 */
export const SNMP_CONTACT_ENV = 'NOC_SNMP_CONTACT';
/** Lands inside a RouterOS set, an airOS sed edit and a jq string: closed set. */
export const SNMP_CONTACT_RE = /^[A-Za-z0-9][A-Za-z0-9 .,:@&()_+-]{0,63}$/;

/** Networks allowed to poll, comma separated. Org data: configuration, never a literal here. */
export const SNMP_COMMUNITY_ADDRESSES_ENV = 'NOC_SNMP_COMMUNITY_ADDRESSES';
/** RouterOS "any local address" for `/snmp src-address`. */
export const SNMP_SRC_ADDRESS_ALL = '::';

/**
 * Which credential pools a vendor may use, in order. Infrastructure first (the
 * exact credentials the read path uses), then the CPE pool: the devices this
 * exists for are overwhelmingly customer radios, which usually share the same
 * admin password family but live in their own pool. Both are still bounded by
 * NOC_WRITE_SNMP_MAX_POOL_TRIES.
 */
export const VENDOR_DEVICE_TYPES: Record<SnmpConfig['vendor'], DeviceType[]> = {
  mikrotik: ['mikrotik-ssh', 'customer-mt'],
  airos: ['ubnt-airos', 'customer-airos'],
  // WaveOS radios have their own SSH pool (Buildroot `ucfg`, not airOS system.cfg).
  wave: ['ubnt-wave'],
};

/** Round-robin across pools so a 3-try budget still reaches every pool once. */
export function interleavePoolCandidates<T>(lists: T[][]): T[] {
  const out: T[] = [];
  const max = Math.max(0, ...lists.map((l) => l.length));
  for (let i = 0; i < max; i += 1) {
    for (const list of lists) if (list[i] !== undefined) out.push(list[i]);
  }
  return out;
}

export const GpsSchema = z.object({
  lat: z.number().min(-90).max(90),
  lon: z.number().min(-180).max(180),
});
export type GpsFix = z.infer<typeof GpsSchema>;

export const SnmpConfigSchema = z.object({
  op: z.enum(SNMP_OPS),
  vendor: z.enum(SNMP_VENDORS),
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
  reason: ReasonSchema,
  execute: z.boolean().default(false),
  /**
   * THIS device's own GPS fix, read by the caller (a Wave reports it on its own
   * read). Omitted means sysLocation is left exactly as it is: a coordinate
   * this tool invented would be worse than no coordinate.
   */
  gps: GpsSchema.optional(),
});
export type SnmpConfig = z.infer<typeof SnmpConfigSchema>;

/**
 * Community strings land inside a RouterOS command and an airOS sed edit, so the
 * character set is closed. 8 characters minimum: shorter values are in every
 * scanner wordlist.
 */
export const SNMP_COMMUNITY_RE = /^[A-Za-z0-9][A-Za-z0-9_.:@-]{7,63}$/;
export const COMMUNITY_REDACTED = '<SNMP community redacted>';
export const POOL_TRIES_MIN = 1;
export const POOL_TRIES_MAX = 5;
export const POOL_TRIES_DEFAULT = 3;
/** RouterOS prints an unbounded list; keep the command line sane and reviewable. */
export const MAX_COMMUNITY_ADDRESSES = 32;

export interface SnmpSettings {
  community: string;
  /** Networks allowed to poll (`addresses=`). Empty = no restriction is installed. */
  addresses: string[];
  /** sysContact written to every device (NOC_SNMP_CONTACT). Undefined = leave it alone. */
  contact?: string;
}

/**
 * LibreNMS renders a map pin from `[lat, lon]` in sysLocation, so that is the
 * only form worth writing. Six decimals is ~0.1 m — past the GPS fix itself.
 */
export function formatSnmpLocation(gps: GpsFix): string {
  const fmt = (n: number) => {
    const s = n.toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
    return s === '-0' ? '0' : s;
  };
  return `[${fmt(gps.lat)}, ${fmt(gps.lon)}]`;
}

export type SnmpSettingsResult = { ok: true; settings: SnmpSettings } | { ok: false; error: string };

/**
 * Parse the poller list. Values go straight into a RouterOS command, so each
 * entry must be a canonical IPv4 address or a 1-32 bit CIDR. 0.0.0.0/0 is
 * refused: "every source" is what leaving the variable unset already means, and
 * writing it into the standard config hides that it was never decided.
 */
export function parseCommunityAddresses(csv: string | undefined): { ok: true; addresses: string[] } | { ok: false; error: string } {
  const out: string[] = [];
  for (const raw of (csv ?? '').split(',')) {
    const entry = raw.trim();
    if (!entry) continue;
    const slash = entry.indexOf('/');
    const base = slash === -1 ? entry : entry.slice(0, slash);
    if (parseIPv4(base) === null) {
      return { ok: false, error: `${SNMP_COMMUNITY_ADDRESSES_ENV} entry "${entry}" is not a canonical IPv4 address or CIDR (no leading zeros, exactly four octets)` };
    }
    if (slash !== -1) {
      const prefix = entry.slice(slash + 1);
      if (!/^\d{1,2}$/.test(prefix) || Number(prefix) < 1 || Number(prefix) > 32) {
        return { ok: false, error: `${SNMP_COMMUNITY_ADDRESSES_ENV} entry "${entry}" has a prefix outside 1-32; leave the variable unset instead of using 0.0.0.0/0` };
      }
    }
    if (!out.includes(entry)) out.push(entry);
  }
  if (out.length > MAX_COMMUNITY_ADDRESSES) {
    return { ok: false, error: `${SNMP_COMMUNITY_ADDRESSES_ENV} has ${out.length} entries; at most ${MAX_COMMUNITY_ADDRESSES} belong in a device community` };
  }
  return { ok: true, addresses: out };
}

/** Read the community and the poller list. Neither value ever comes from the caller. */
export function readSnmpSettings(env: NodeJS.ProcessEnv = process.env): SnmpSettingsResult {
  const community = (env.NOC_SNMP_COMMUNITY ?? '').trim();
  if (!community) {
    return { ok: false, error: 'NOC_SNMP_COMMUNITY is unset. The community is read from the environment, never from a tool call or a command line.' };
  }
  if (/^(public|private)$/i.test(community)) {
    return { ok: false, error: 'NOC_SNMP_COMMUNITY must not be public/private: those are the first strings any scanner tries.' };
  }
  if (!SNMP_COMMUNITY_RE.test(community)) {
    return { ok: false, error: 'NOC_SNMP_COMMUNITY must be 8-64 characters of letters, digits or _ . : @ - and start with a letter or digit.' };
  }
  const parsed = parseCommunityAddresses(env[SNMP_COMMUNITY_ADDRESSES_ENV]);
  if (!parsed.ok) return { ok: false, error: parsed.error };
  const contact = (env[SNMP_CONTACT_ENV] ?? '').trim();
  if (contact && !SNMP_CONTACT_RE.test(contact)) {
    return { ok: false, error: `${SNMP_CONTACT_ENV} must be 1-64 characters of letters, digits, space or . , : @ & ( ) _ + - and start with a letter or digit; leave it unset rather than guess a contact` };
  }
  return { ok: true, settings: { community, addresses: parsed.addresses, ...(contact ? { contact } : {}) } };
}

/**
 * A verify dump contains community values, and those may belong to someone
 * else's monitoring (an old NOC, the vendor's install). We cannot redact by
 * known-value here — the whole point is that the agent never learns the value —
 * so redact by shape: the field after `name=` / `trap-community:` on RouterOS
 * and the whole `snmp.community=` line on airOS. `addresses=` stays readable:
 * it is the poller list, and an audit needs exactly that.
 */
export function redactVerifyOutput(text: string, vendor: SnmpConfig['vendor'] = 'mikrotik'): string {
  if (!text) return text;
  if (vendor === 'mikrotik') {
    return text
      .replace(/(\bname=)(?:"[^"]*"|\S+)/gi, '$1<redacted>')
      .replace(/(\btrap-community:\s*)(\S+)/gi, '$1<redacted>');
  }
  if (vendor === 'wave') {
    // WaveOS `ucfg show` is JSON (`services.snmp.community`), so redact the JSON
    // key form and, as a backstop, the dotted jq-assignment form a plan echoes.
    return text
      .replace(/("(?:ro)?communit(?:y|ies)"\s*:\s*)"[^"]*"/gi, '$1"<redacted>"')
      .replace(/(\.services\.snmp\.(?:ro)?communit(?:y|ies)\s*=\s*)"[^"]*"/gi, '$1"<redacted>"');
  }
  // airOS has more than one community key (`snmp.community`, `snmp.rocommunity`,
  // trap variants on some builds), so match the whole family: a single-key rule
  // printed a live community string in an audit read-back.
  return text.replace(/^(\s*snmp\.[A-Za-z0-9_]*community=).*$/gim, '$1<redacted>');
}

/** Replace the community in any text we return, log or audit. */
export function redactCommunity(text: string, community: string | undefined): string {
  if (!community || !text) return text;
  return text.split(community).join(COMMUNITY_REDACTED);
}

/** The `/snmp src-address` value from a `/snmp print`, or null when not printed. */
export function readSnmpSrcAddress(text: string): string | null {
  const match = /^\s*src-address:\s*(\S*)/m.exec(text ?? '');
  return match ? match[1] : null;
}

/**
 * A src-address that is set at all is a liability: it pins the daemon to one
 * local address, and once that address is renumbered off the device SNMP stops
 * answering while `enabled: yes` still shows. `::` (any local address) is the
 * only value that survives a renumber.
 */
export function needsSrcAddressReset(text: string): boolean {
  const value = readSnmpSrcAddress(text);
  return value !== null && value !== '' && value !== SNMP_SRC_ADDRESS_ALL;
}

export interface SnmpPlan {
  valid: boolean;
  error?: string;
  op?: SnmpConfig['op'];
  vendor?: SnmpConfig['vendor'];
  /** Read the SNMP service state and the community list in one round trip. */
  probeCmd?: string;
  /** mikrotik: apply the standard to a community that already carries our name. */
  communitySetCmd?: string;
  /** mikrotik: create our community when none carries the name. */
  communityAddCmd?: string;
  /** mikrotik: clear a configured src-address (see needsSrcAddressReset). */
  srcAddressCmd?: string;
  /** mikrotik: turn the service on and point traps at our community. */
  enableCmd?: string;
  /** Read-back for both vendors (RouterOS print, airOS grep). */
  verifyCmd?: string;
  /** aios: the exact system.cfg keys to set (these must already exist). */
  edits?: CfgEdit[];
  /** aios: keys set only when the build already carries the line (sed cannot create one). */
  optionalEdits?: CfgEdit[];
  /** wave: `ucfg edit` jq filters, in apply order (community first, then enable). */
  waveEdits?: string[];
  /** mikrotik: set sysLocation from a GPS fix (`[lat, lon]`); absent = leave it alone. */
  locationCmd?: string;
  /** The sysContact this plan installs (visible on purpose: not a secret). */
  contact?: string;
  /** The sysLocation this plan installs, when the caller supplied a GPS fix. */
  location?: string;
  /** wave: read the live config (`ucfg show`). */
  waveReadCmd?: string;
  /** wave: persist the running config (`ucfg commit`). */
  waveCommitCmd?: string;
  note: string;
}

const ROUTEROS_SNMP_VERIFY = '/snmp print ; /snmp community print detail';
// Every snmp.* key, not just the two we write: an audit needs to see whether
// traps/contact/location are set too. `snmp.community=` is redacted by shape.
const AIROS_SNMP_VERIFY = "grep -E '^snmp\\.' /tmp/system.cfg";

export function planSnmpConfig(input: SnmpConfig, settings?: SnmpSettings): SnmpPlan {
  const refuse = (error: string): SnmpPlan => ({ valid: false, error, note: '' });

  if (input.op === 'snmp-verify') {
    return {
      valid: true,
      op: input.op,
      vendor: input.vendor,
      verifyCmd: input.vendor === 'mikrotik' ? ROUTEROS_SNMP_VERIFY : input.vendor === 'wave' ? WAVE_READ_CMD : AIROS_SNMP_VERIFY,
      note: 'Read-only: returns every snmp setting it can see (RouterOS: service state, `src-address`, communities; airOS: all `snmp.*` keys). Community values are redacted by shape, `addresses=` stays visible. No write, no gates needed.',
    };
  }

  if (!settings) return refuse('snmp-enable needs NOC_SNMP_COMMUNITY');
  if (!SNMP_COMMUNITY_RE.test(settings.community)) return refuse('community is not in the accepted character set');

  if (input.vendor === 'mikrotik') {
    const name = `name="${settings.community}"`;
    // No addresses= when the poller list is unset: installing a guessed list is
    // how a device ends up enabled and still unpollable.
    const addresses = settings.addresses.length ? ` addresses=${settings.addresses.join(',')}` : '';
    const location = input.gps ? formatSnmpLocation(input.gps) : undefined;
    // Contact rides along with the enable when it is configured; with none the
    // command is the same minus the attribute, so nothing is guessed.
    const contact = settings.contact ? ` contact="${settings.contact}"` : '';
    return {
      valid: true,
      op: input.op,
      vendor: input.vendor,
      probeCmd: ROUTEROS_SNMP_VERIFY,
      communitySetCmd: `/snmp community set [find ${name}] security=none${addresses}`,
      communityAddCmd: `/snmp community add ${name} security=none${addresses}`,
      srcAddressCmd: `/snmp set src-address=${SNMP_SRC_ADDRESS_ALL}`,
      // contact rides along with the enable: one round trip, no separate step to fail.
      enableCmd: `/snmp set enabled=yes trap-community="${settings.community}"${contact}`,
      locationCmd: location ? `/snmp set location="${location}"` : undefined,
      contact: settings.contact,
      location,
      verifyCmd: ROUTEROS_SNMP_VERIFY,
      note:
        'Applies the standard RouterOS SNMP config: one read-only community (`security=none`) carrying our name, '
        + (settings.addresses.length
          ? `the poller sources in addresses= (${settings.addresses.length} entries from ${SNMP_COMMUNITY_ADDRESSES_ENV}), `
          : `no addresses= because ${SNMP_COMMUNITY_ADDRESSES_ENV} is unset (the community accepts any source), `)
        + 'traps pointed at that community, and a configured `src-address` reset to `::` first — a src-address the device no longer owns is the usual reason SNMP is "enabled" and silent. '
        + 'A community carrying our name is re-applied to the standard rather than trusted; other communities are never touched. '
        + `sysContact is written when ${SNMP_CONTACT_ENV} is configured (${settings.contact ?? 'unset, left as it is'}), and sysLocation to the caller's GPS fix as \`[lat, lon]\` — the form LibreNMS puts on its map — only when a fix was supplied; otherwise location is left as it is. Read-back follows the apply; `
        + 'confirming LibreNMS can poll is the real proof.',
    };
  }

  if (input.vendor === 'wave') {
    // WaveOS keeps SNMP in `services.snmp` (community/contact/location/enabled).
    // Live reads of both a polled Wave and an unconfigured one confirm exactly
    // those four keys; an unconfigured radio carries only `enabled: false`.
    return {
      valid: true,
      op: input.op,
      vendor: input.vendor,
      waveReadCmd: WAVE_READ_CMD,
      waveCommitCmd: WAVE_COMMIT_CMD,
      verifyCmd: WAVE_READ_CMD,
      waveEdits: waveSnmpFilters(settings.community, settings.contact, input.gps ? formatSnmpLocation(input.gps) : undefined),
      contact: settings.contact,
      location: input.gps ? formatSnmpLocation(input.gps) : undefined,
      note: 'Sets services.snmp.community and services.snmp.enabled on the Wave through `ucfg edit` (which applies to the RUNNING config immediately) and persists with `ucfg commit`. '
        + 'The community is written first: an enabled daemon holding the wrong community is the state that looks configured and polls nothing. '
        + `services.snmp.contact is written when ${SNMP_CONTACT_ENV} is configured (${settings.contact ?? 'unset, left as it is'}); services.snmp.location to this radio's own GPS fix as \`[lat, lon]\` when one was supplied, and left untouched when it was not. `
        + 'Re-read follows the edit and again after the commit; the radio restarts services on commit, so the post-commit read is retried. '
        + 'There is no src-address equivalent on WaveOS.',
    };
  }

  return {
    valid: true,
    op: input.op,
    vendor: input.vendor,
    verifyCmd: AIROS_SNMP_VERIFY,
    edits: [
      { key: 'snmp.status', value: 'enabled' },
      { key: 'snmp.community', value: settings.community },
    ],
    // Real infrastructure radios carry snmp.rocommunity too, set to the same
    // polling community; builds that lack the line are not considered broken.
    optionalEdits: [
      { key: 'snmp.rocommunity', value: settings.community },
      ...(settings.contact ? [{ key: 'snmp.contact' as const, value: settings.contact }] : []),
      ...(input.gps ? [{ key: 'snmp.location' as const, value: formatSnmpLocation(input.gps) }] : []),
    ],
    contact: settings.contact,
    location: input.gps ? formatSnmpLocation(input.gps) : undefined,
    note:      'Sets snmp.status/snmp.community in /tmp/system.cfg through the snapshot + softrestart-save transport, also keeps snmp.rocommunity equal to the same community where the build '
      + 'carries that line, re-reads every snmp key it wrote, and restores the snapshot on any mismatch. The device must already carry the status/community lines (every airOS '
      + 'build seen so far does); if it does not, the tool refuses before writing and SNMP has to be switched on once from the web UI. snmp.contact (the configured sysContact) and '
      + `snmp.location (\`[lat, lon]\` from a supplied GPS fix) are written only where the build already carries those lines — the read-back reports a build without them. `
      + 'Some airOS builds only start the SNMP daemon on a full reboot — the tool never reboots a radio.',
  };
}

/** Render a plan with the community removed (safe to return to the agent). */
export function redactPlan(plan: SnmpPlan, community?: string): SnmpPlan {
  if (!community) return plan;
  const scrub = (s: string | undefined) => (s === undefined ? undefined : redactCommunity(s, community));
  return {
    ...plan,
    communitySetCmd: scrub(plan.communitySetCmd),
    communityAddCmd: scrub(plan.communityAddCmd),
    enableCmd: scrub(plan.enableCmd),
    edits: plan.edits?.map((e) => (e.key === 'snmp.community' ? { ...e, value: COMMUNITY_REDACTED } : e)),
    optionalEdits: plan.optionalEdits?.map((e) => (e.key === 'snmp.rocommunity' ? { ...e, value: COMMUNITY_REDACTED } : e)),
    waveEdits: plan.waveEdits?.map((f) => scrub(f) ?? f),
  };
}

/**
 * The write gates. `snmp-verify` is a read and skips these on purpose: auditing
 * an estate must not require the write kill switch.
 */
export function checkSnmpWriteGates(host: string): { ok: true } | { ok: false; error: string } {
  if (process.env.NOC_WRITE_ENABLED !== 'true') {
    return { ok: false, error: 'Writes disabled; require NOC_WRITE_ENABLED=true (kill switch, default off)' };
  }
  if (process.env.NOC_WRITE_SNMP_TRANSPORT !== 'verified') {
    return {
      ok: false,
      error: 'SNMP write transport not verified; run snmp-enable against one spare MikroTik and one spare airOS device first, then set NOC_WRITE_SNMP_TRANSPORT=verified',
    };
  }
  return checkWriteHost(host);
}

/** Pool credentials are the connector's own device-type credentials. Opt-in, twice over. */
export function poolCredentialsEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.NOC_WRITE_SNMP_POOL_CREDENTIALS === 'true';
}

export function poolCredentialTries(env: NodeJS.ProcessEnv = process.env): number {
  const raw = (env.NOC_WRITE_SNMP_MAX_POOL_TRIES ?? '').trim();
  const n = /^\d{1,2}$/.test(raw) ? Number(raw) : POOL_TRIES_DEFAULT;
  if (!Number.isFinite(n) || n < POOL_TRIES_MIN) return POOL_TRIES_MIN;
  return Math.min(POOL_TRIES_MAX, n);
}

/**
 * RouterOS answers a refused command in the output, not with an exit code, so a
 * failed SNMP step must throw or the caller would claim success.
 */
const COMMAND_FAILURE_RE = /syntax error|bad command name|no such item|invalid value|failure:|not enough permission/i;

export interface SnmpCommandResult {
  cmd: string;
  result: string;
}

export interface SnmpExecuteResult {
  applied: boolean;
  /** True when a community with our name already existed and was re-applied to the standard. */
  communityExisted: boolean;
  /** True when a configured src-address was cleared to :: (the silent-SNMP fix). */
  srcAddressReset: boolean;
  commands: SnmpCommandResult[];
  verify: string;
  plan: SnmpPlan;
}

export async function executeMikrotikSnmp(
  run: (cmd: string) => Promise<string>,
  settings: SnmpSettings,
  plan: SnmpPlan,
): Promise<SnmpExecuteResult> {
  if (!plan.valid || !plan.probeCmd || !plan.communitySetCmd || !plan.communityAddCmd || !plan.srcAddressCmd || !plan.enableCmd || !plan.verifyCmd) {
    throw new Error(`Plan invalid: ${plan.error ?? 'incomplete mikrotik plan'}`);
  }
  const probe = await run(plan.probeCmd);
  // RouterOS prints `name="<community>"` in `print detail`; match the quoted form
  // only, so a community that merely contains our name does not count as ours.
  const communityExisted = probe.includes(`name="${settings.community}"`);
  const srcAddressReset = needsSrcAddressReset(probe);
  const steps = [
    // src-address first: if the daemon is pinned to a dead address, nothing after
    // this point is reachable and every later failure would mislead the caller.
    ...(srcAddressReset ? [plan.srcAddressCmd] : []),
    communityExisted ? plan.communitySetCmd : plan.communityAddCmd,
    plan.enableCmd,
    ...(plan.locationCmd ? [plan.locationCmd] : []),
  ];
  const commands: SnmpCommandResult[] = [];
  for (const cmd of steps) {
    const result = await run(cmd);
    commands.push({ cmd, result });
    if (COMMAND_FAILURE_RE.test(result)) {
      throw new Error(`RouterOS rejected an SNMP step: ${result.trim().slice(0, 300)}`);
    }
  }
  const verify = await run(plan.verifyCmd);
  return { applied: true, communityExisted, srcAddressReset, commands, verify, plan };
}

/* ------------------------------------------------------------------ WaveOS */

/** WaveOS live config read. */
export const WAVE_READ_CMD = 'ucfg show';
/** WaveOS persist: `ucfg edit` changes the RUNNING config, commit makes it survive a reboot. */
export const WAVE_COMMIT_CMD = 'ucfg commit';

/**
 * The only two edits the Wave arm may apply. The dump of a polled Wave and one
 * with SNMP never configured differ in exactly these keys (`services.snmp` also
 * carries contact/location, which are read-only here).
 */
export function waveSnmpFilters(community: string, contact?: string, location?: string): string[] {
  if (!SNMP_COMMUNITY_RE.test(community)) throw new Error('community is not in the accepted character set');
  if (contact !== undefined && !SNMP_CONTACT_RE.test(contact)) throw new Error('contact is not in the accepted character set');
  // JSON.stringify gives the double-quoted jq string; both charsets above exclude
  // quote and backslash, so every filter stays well-formed.
  const filters = [`.services.snmp.community = ${JSON.stringify(community)}`];
  if (contact !== undefined) filters.push(`.services.snmp.contact = ${JSON.stringify(contact)}`);
  if (location !== undefined) {
    // Only the form LibreNMS maps. Anything else belongs in sysDescr, not here.
    if (!/^\[-?\d+(\.\d+)?, -?\d+(\.\d+)?\]$/.test(location)) throw new Error('location must be [lat, lon]');
    filters.push(`.services.snmp.location = ${JSON.stringify(location)}`);
  }
  // Enable last: the daemon must come up already holding the right identity.
  filters.push('.services.snmp.enabled = true');
  return filters;
}

export interface WaveSnmpState {
  enabled: boolean | null;
  community: string | null;
  contact: string | null;
  location: string | null;
}

/**
 * What a Wave read may show us: `services.snmp` and the hostname, nothing else.
 *
 * `ucfg show` is the WHOLE radio config — it also carries `services.uisp.key`,
 * `wireless.encryption.passphrase` and the user password hash. Returning that
 * dump put a live UISP API key and a link PSK into tool output (field incident),
 * so the Wave arm never returns the dump: it parses the JSON and projects just
 * the SNMP service (community redacted by shape).
 */
export function waveSnmpOnlyView(text: string): string {
  const start = (text ?? '').indexOf('{');
  if (start === -1) return 'ucfg show returned no JSON; nothing displayed';
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.slice(start));
  } catch {
    return 'ucfg show output was not valid WaveOS JSON; nothing displayed';
  }
  const cfg = parsed as { services?: { snmp?: Record<string, unknown> }; system?: { hostname?: string } };
  const view = {
    hostname: cfg?.system?.hostname ?? null,
    services: { snmp: cfg?.services?.snmp ?? null },
  };
  return redactVerifyOutput(JSON.stringify(view, null, 2), 'wave');
}

/** Read `services.snmp` out of a `ucfg show` dump. Throws when the dump is not WaveOS JSON. */
export function readWaveSnmpState(text: string): WaveSnmpState {
  const start = (text ?? '').indexOf('{');
  if (start === -1) throw new Error('ucfg show returned no JSON; refusing (not a WaveOS radio?)');
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.slice(start));
  } catch {
    throw new Error('ucfg show output was not valid WaveOS JSON; refusing');
  }
  const snmp = (parsed as { services?: { snmp?: Record<string, unknown> } })?.services?.snmp;
  return {
    enabled: typeof snmp?.enabled === 'boolean' ? snmp.enabled : null,
    community: typeof snmp?.community === 'string' ? snmp.community : null,
    contact: typeof snmp?.contact === 'string' ? snmp.contact : null,
    location: typeof snmp?.location === 'string' ? snmp.location : null,
  };
}

export interface WaveExecuteResult {
  /** The live running config was verified to hold community + enabled. */
  applied: boolean;
  /** Persistence proven by a post-commit re-read (a commit that drops the session still may have landed). */
  committed: boolean;
  postCommitVerified: boolean;
  /** True when `ucfg commit` returned without error, even if the re-read was lost. */
  commitReported: boolean;
  communityChanged: boolean;
  contactChanged: boolean;
  /** The `[lat, lon]` written this run; null when no GPS fix was supplied. */
  locationSet: string | null;
  wasEnabled: boolean;
  commands: SnmpCommandResult[];
  verify: string;
  plan: SnmpPlan;
}

const waveSleep = (ms: number) => new Promise<void>((resolve) => { setTimeout(resolve, ms); });

/**
 * Apply the Wave SNMP config.
 *
 * Order matters: community first, then enable. The opposite order leaves an
 * enabled daemon holding whatever community it had — the state that looks
 * configured and polls nothing, which is exactly what this tool exists to end.
 *
 * `ucfg edit` is live, so the re-read BEFORE the commit is the proof the change
 * took effect; `ucfg commit` only persists and restarts services (it can drop
 * the SSH session, so its exit is not the source of truth — the post-commit
 * re-read is).
 */
export async function executeWaveSnmp(
  run: (cmd: string) => Promise<string>,
  settings: SnmpSettings,
  plan: SnmpPlan,
  options?: { postCommitDelaysMs?: number[] },
): Promise<WaveExecuteResult> {
  if (!SNMP_COMMUNITY_RE.test(settings.community)) throw new Error('community is not in the accepted character set');
  const readCmd = plan.waveReadCmd ?? WAVE_READ_CMD;
  const commitCmd = plan.waveCommitCmd ?? WAVE_COMMIT_CMD;

  const before = await run(readCmd);
  const state = readWaveSnmpState(before);
  const communityChanged = state.community !== settings.community;
  const contactChanged = state.contact !== settings.contact;
  // No fix supplied means the existing location is none of our business.
  const locationSet = plan.location ?? null;
  const locationChanged = locationSet !== null && state.location !== locationSet;
  const wasEnabled = state.enabled === true;

  const filters: string[] = [];
  if (communityChanged) filters.push(`.services.snmp.community = ${JSON.stringify(settings.community)}`);
  if (contactChanged) filters.push(`.services.snmp.contact = ${JSON.stringify(settings.contact)}`);
  if (locationChanged) filters.push(`.services.snmp.location = ${JSON.stringify(locationSet)}`);
  if (!wasEnabled) filters.push('.services.snmp.enabled = true');
  const matchesTarget = (s: WaveSnmpState) => s.enabled === true
    && s.community === settings.community
    && (!contactChanged || s.contact === settings.contact)
    && (!locationChanged || s.location === locationSet);

  const commands: SnmpCommandResult[] = [];
  for (const filter of filters) {
    const cmd = `ucfg edit '${filter}'`;
    const result = await run(cmd);
    commands.push({ cmd, result });
    if (/error|invalid|unknown option|syntax error/i.test(result)) {
      throw new Error(`WaveOS rejected an SNMP step: ${result.trim().slice(0, 200)}`);
    }
  }

  if (!filters.length) {
    // Already at the standard: nothing to write, nothing to commit.
    return {
      applied: true, committed: false, postCommitVerified: true, commitReported: true,
      communityChanged, contactChanged, locationSet, wasEnabled, commands, verify: waveSnmpOnlyView(before), plan,
    };
  }

  const liveText = await run(readCmd);
  const live = readWaveSnmpState(liveText);
  if (!matchesTarget(live)) {
    throw new Error('WaveOS re-read did not show the SNMP change; nothing was committed');
  }

  let commitTrouble: string | null = null;
  try {
    commands.push({ cmd: commitCmd, result: await run(commitCmd) });
  } catch (e) {
    commitTrouble = e instanceof Error ? e.message : String(e);
    commands.push({ cmd: commitCmd, result: `session ended during commit: ${commitTrouble.slice(0, 160)}` });
  }

  const delays = options?.postCommitDelaysMs ?? [6000, 6000, 6000];
  let post = liveText;
  let postCommitVerified = false;
  for (const delay of delays) {
    await waveSleep(delay);
    try {
      post = await run(readCmd);
      const after = readWaveSnmpState(post);
      if (matchesTarget(after)) { postCommitVerified = true; break; }
    } catch {
      // The radio restarts services on commit; a failed read here is expected.
    }
  }

  return {
    applied: true, committed: postCommitVerified, postCommitVerified, commitReported: commitTrouble === null,
    communityChanged, contactChanged, locationSet, wasEnabled, commands, verify: waveSnmpOnlyView(post), plan,
  };
}
