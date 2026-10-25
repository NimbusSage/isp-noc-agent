/**
 * Strict host/address handling shared by every tool that connects to a device.
 *
 * Why this exists (security audit, Lane 2b):
 *  - IPv4 literals with leading zeros ("010.1.2.3") are octal to libc
 *    (8.1.2.3) but decimal to naive parsers; every host argument and every secret-scan:allow(octal-address pitfall example)
 *    allowlist entry must be the canonical dotted-quad form or it is rejected.
 *  - A CIDR entry such as "10.0.0.0/" used to parse its prefix as
 *    Number('') === 0, i.e. /0, which allows every address. Prefixes must be
 *    1-2 digit (IPv4) integers and nothing shorter than /8 is accepted unless
 *    ALLOW_BROAD_CIDR=1 is set explicitly.
 *  - Malformed allowlist entries fail CLOSED (throw) instead of being skipped.
 *  - Read tools accepted any host. NOC_READ_ALLOWED_HOSTS limits where the
 *    server will aim credential attempts; the default is private/link-local
 *    space only, so an injected agent cannot point the credential pool at an
 *    attacker-controlled public host.
 */
import { isIPv6 } from 'node:net';
import { registeredHostsForDeviceType } from '../config/resources.js';

export const IPV4_OCTET = '(?:25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)';
/** Canonical dotted quad: no leading zeros, no whitespace, no short forms. */
export const IPV4_CANONICAL_RE = new RegExp(`^(?:${IPV4_OCTET}\\.){3}${IPV4_OCTET}$`);

const HOSTNAME_LABEL = '[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?';
const HOSTNAME_RE = new RegExp(`^${HOSTNAME_LABEL}(?:\\.${HOSTNAME_LABEL})*$`);

export class HostListError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'HostListError';
  }
}

export function parseIPv4(s: string): number | null {
  if (!IPV4_CANONICAL_RE.test(s)) return null;
  const [a, b, c, d] = s.split('.').map(Number);
  return ((a << 24) | (b << 16) | (c << 8) | d) >>> 0;
}

/** IPv6 literal (hex and colons only: no zone id, no embedded IPv4). */
export function parseIPv6(s: string): bigint | null {
  if (!/^[0-9A-Fa-f:]+$/.test(s) || !isIPv6(s)) return null;
  const halves = s.split('::');
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(':') : [];
  const tail = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  let groups: string[];
  if (halves.length === 1) {
    if (head.length !== 8) return null;
    groups = head;
  } else {
    const fill = 8 - head.length - tail.length;
    if (fill < 1) return null;
    groups = [...head, ...Array<string>(fill).fill('0'), ...tail];
  }
  let value = 0n;
  for (const g of groups) {
    if (!/^[0-9A-Fa-f]{1,4}$/.test(g)) return null;
    value = (value << 16n) | BigInt(`0x${g}`);
  }
  return value;
}

/**
 * Syntax check for a host argument that will be handed to a connector or
 * pasted into a command. Returns an error string, or null when acceptable.
 * Accepts: canonical IPv4, IPv6 literal, or an RFC 1123 hostname.
 */
export function hostSyntaxError(host: string): string | null {
  if (host.length < 1 || host.length > 253) return 'Host must be 1-253 characters';
  if (/^[0-9.]+$/.test(host)) {
    return parseIPv4(host) === null
      ? 'Host looks like an IPv4 address but is not canonical (no leading zeros, exactly four octets 0-255)'
      : null;
  }
  if (host.includes(':')) {
    return parseIPv6(host) === null ? 'Host looks like an IPv6 address but is not a valid literal (hex and colons only)' : null;
  }
  if (!HOSTNAME_RE.test(host)) return 'Host contains characters outside letters, digits, dots and hyphens';
  // A numeric/hex final label ("0x8.8.8.8", "8.8.8.010") is an IPv4 spelling to
  // inet_aton-style resolvers; real TLDs are never numeric, so refuse it.
  const lastLabel = host.slice(host.lastIndexOf('.') + 1);
  if (/^(?:0[xX][0-9a-fA-F]*|\d+)$/.test(lastLabel)) return 'Host ends in a numeric label (ambiguous IPv4 spelling)';
  return null;
}

export type HostEntry =
  | { kind: 'v4'; base: number; mask: number }
  | { kind: 'v6'; base: bigint; bits: number }
  | { kind: 'name'; name: string };

export interface HostListOptions {
  allowIPv6: boolean;
  allowNames: boolean;
  /** Permit prefixes shorter than /8 (including /0). Off unless ALLOW_BROAD_CIDR=1. */
  allowBroad: boolean;
}

export function broadCidrAllowed(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.ALLOW_BROAD_CIDR === '1';
}

function v4Mask(bits: number): number {
  return bits === 0 ? 0 : (~0 << (32 - bits)) >>> 0;
}

function v6Mask(bits: number): bigint {
  if (bits === 0) return 0n;
  return ((1n << BigInt(bits)) - 1n) << BigInt(128 - bits);
}

/**
 * Parse a comma-separated allowlist. Throws HostListError on ANY malformed
 * entry (fail closed): a typo must never silently widen or narrow scope.
 * Empty items (stray commas/whitespace) are ignored.
 */
export function parseHostList(csv: string | undefined, opts: HostListOptions): HostEntry[] {
  const out: HostEntry[] = [];
  for (const raw of (csv ?? '').split(',')) {
    const entry = raw.trim();
    if (!entry) continue;
    const slash = entry.indexOf('/');
    if (slash === -1) {
      const v4 = parseIPv4(entry);
      if (v4 !== null) { out.push({ kind: 'v4', base: v4, mask: v4Mask(32) }); continue; }
      const v6 = opts.allowIPv6 ? parseIPv6(entry) : null;
      if (v6 !== null) { out.push({ kind: 'v6', base: v6, bits: 128 }); continue; }
      if (opts.allowNames && !/^[0-9.]+$/.test(entry) && !entry.includes(':') && HOSTNAME_RE.test(entry) && entry.length <= 253) {
        out.push({ kind: 'name', name: entry.toLowerCase() });
        continue;
      }
      throw new HostListError(`malformed entry "${entry}" (expected canonical IPv4${opts.allowIPv6 ? ', IPv6' : ''}${opts.allowNames ? ', hostname' : ''} or CIDR)`);
    }
    const base = entry.slice(0, slash);
    const prefix = entry.slice(slash + 1);
    const v4 = parseIPv4(base);
    if (v4 !== null) {
      if (!/^\d{1,2}$/.test(prefix)) throw new HostListError(`malformed CIDR prefix in "${entry}" (1-2 digits required)`);
      const bits = Number(prefix);
      if (bits > 32) throw new HostListError(`CIDR prefix out of range in "${entry}" (0-32)`);
      if (bits < 8 && !opts.allowBroad) throw new HostListError(`CIDR "${entry}" is broader than /8; set ALLOW_BROAD_CIDR=1 only if that is truly intended`);
      out.push({ kind: 'v4', base: v4, mask: v4Mask(bits) });
      continue;
    }
    const v6 = opts.allowIPv6 ? parseIPv6(base) : null;
    if (v6 !== null) {
      if (!/^\d{1,3}$/.test(prefix)) throw new HostListError(`malformed CIDR prefix in "${entry}" (1-3 digits required)`);
      const bits = Number(prefix);
      if (bits > 128) throw new HostListError(`CIDR prefix out of range in "${entry}" (0-128)`);
      // /7 is the narrowest IPv6 block we need to allow (fc00::/7, unique-local).
      if (bits < 7 && !opts.allowBroad) throw new HostListError(`CIDR "${entry}" is broader than /7; set ALLOW_BROAD_CIDR=1 only if that is truly intended`);
      out.push({ kind: 'v6', base: v6, bits });
      continue;
    }
    throw new HostListError(`malformed CIDR base address in "${entry}"`);
  }
  return out;
}

/** True when `host` (canonical IPv4, IPv6 literal, or hostname) is covered by an entry. */
export function hostMatches(entries: HostEntry[], host: string): boolean {
  const v4 = parseIPv4(host);
  if (v4 !== null) {
    return entries.some((e) => e.kind === 'v4' && ((v4 & e.mask) >>> 0) === ((e.base & e.mask) >>> 0));
  }
  const v6 = host.includes(':') ? parseIPv6(host) : null;
  if (v6 !== null) {
    return entries.some((e) => e.kind === 'v6' && (v6 & v6Mask(e.bits)) === (e.base & v6Mask(e.bits)));
  }
  if (hostSyntaxError(host) !== null) return false;
  const lower = host.toLowerCase();
  return entries.some((e) => e.kind === 'name' && e.name === lower);
}

export const DEFAULT_READ_ALLOWED_HOSTS = [
  '10.0.0.0/8',
  '172.16.0.0/12',
  '192.168.0.0/16',
  '169.254.0.0/16',
  '127.0.0.0/8',
  '::1/128',
  'fe80::/10',
  'fc00::/7',
].join(',');

export type HostCheck = { ok: true } | { ok: false; error: string };

/**
 * Read-host policy for EVERY tool that opens a connection (read, write, temp-*).
 *
 * NOC_READ_ALLOWED_HOSTS (comma list of exact IPs, CIDRs, and optionally
 * hostnames) REPLACES the default when set. Unset/empty = private, loopback and
 * link-local ranges only. Hostnames are never resolved: they are allowed only
 * when listed verbatim, so a DNS answer can never widen scope.
 *
 * One addition, for the device type that talks to a registered service: the host of that
 * service (site-pack/profile.json "resources", see scripts/resources.py) is allowed for that
 * device type only, so a UISP host name needs no list entry but can never receive router
 * credentials.
 */
export function checkReadHost(host: string, env: NodeJS.ProcessEnv = process.env, deviceType?: string): HostCheck {
  const syntax = hostSyntaxError(host);
  if (syntax) return { ok: false, error: `Host rejected: ${syntax}` };
  const configured = env.NOC_READ_ALLOWED_HOSTS?.trim();
  let entries: HostEntry[];
  try {
    entries = parseHostList(configured ? configured : DEFAULT_READ_ALLOWED_HOSTS, {
      allowIPv6: true,
      allowNames: true,
      // The built-in default is trusted; only operator-supplied lists are range-checked.
      allowBroad: !configured || broadCidrAllowed(env),
    });
  } catch (e) {
    return { ok: false, error: `NOC_READ_ALLOWED_HOSTS is malformed (${e instanceof Error ? e.message : 'invalid'}); all connections are blocked until it is fixed` };
  }
  if (hostMatches(entries, host)) return { ok: true };
  if (deviceType && registeredHostsForDeviceType(deviceType, env).includes(host.toLowerCase())) return { ok: true };
  return {
    ok: false,
    error: `Host ${host} is not permitted by NOC_READ_ALLOWED_HOSTS (default when unset: private, loopback and link-local ranges only)`
      + (deviceType ? `; a ${deviceType} service host must be registered first: python3 scripts/resources.py set <name> <url>` : ''),
  };
}

/** Startup self-check: human-readable problems with the host-list env vars. */
export function hostEnvProblems(env: NodeJS.ProcessEnv = process.env): string[] {
  const problems: string[] = [];
  const broad = broadCidrAllowed(env);
  const checks: Array<[string, HostListOptions]> = [
    ['NOC_READ_ALLOWED_HOSTS', { allowIPv6: true, allowNames: true, allowBroad: broad }],
    ['TEMP_SITE_ALLOWED_HOSTS', { allowIPv6: false, allowNames: false, allowBroad: broad }],
    ['NOC_WRITE_ALLOWED_HOSTS', { allowIPv6: false, allowNames: false, allowBroad: broad }],
  ];
  for (const [name, opts] of checks) {
    const value = env[name];
    if (!value || !value.trim()) continue;
    try {
      parseHostList(value, opts);
    } catch (e) {
      problems.push(`${name}: ${e instanceof Error ? e.message : 'invalid'}`);
    }
  }
  return problems;
}
