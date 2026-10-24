/**
 * Positive allowlists for every value that ends up inside a device command.
 *
 * The previous approach was a deny-list (`/^[^;"'`$\\]+$/`) which still let
 * newlines, `[ ]` (RouterOS command substitution), spaces and `|` through.
 * Everything below is a closed character set instead; every character that is
 * not listed is rejected.
 */
import { z } from 'zod';
import { IPV4_CANONICAL_RE, IPV4_OCTET, hostSyntaxError } from './hosts.js';

/** Interface names, aliases, areas, tags, protocol/rate-set identifiers. */
export const IDENT_RE = /^[A-Za-z0-9_.:-]{1,32}$/;
/** Incident IDs end up inside a RouterOS comment tag (TEMP-<id>-REMOVE-WHEN-REPAIRED). */
export const INCIDENT_ID_RE = /^[A-Za-z0-9_.-]{3,64}$/;
/** RouterOS wireless interface name (wlan1, wlan2, ...). */
export const WLAN_IFACE_RE = /^[A-Za-z0-9_-]{1,16}$/;
/** MikroTik wireless mode (ap-bridge, station-bridge, ...). */
export const MT_MODE_RE = /^[a-z0-9-]{1,32}$/;
/**
 * SSIDs are always emitted inside double quotes (RouterOS) or a sed
 * replacement (airOS). The set excludes every quote, `$`, backslash, bracket,
 * `;`, `|`, `?`, `<`, `>`, `!`, `&`, backtick and control character.
 */
export const SSID_RE = /^[A-Za-z0-9 ._:+@#%()=,/~*-]{1,32}$/;
/** Free-text audit reason: printable ASCII minus quoting/command metacharacters. */
export const REASON_RE = /^[\x20-\x7e]+$/;
const REASON_FORBIDDEN = /[;"'`$\\|[\]]/;
/** Opaque inventory identifier: no control characters. */
export const DEVICE_ID_RE = /^[^\x00-\x1f\x7f]+$/;
/** UISP device IDs are UUIDs; alphanumerics, `_` and `-` only (no `.`/`/`, so no traversal). */
export const UISP_DEVICE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

/** Canonical IPv4 only (no leading zeros). */
export const Ipv4Schema = z.string().regex(IPV4_CANONICAL_RE, 'Exact canonical IPv4 address (no leading zeros)');

const IPV4_BODY = `(?:${IPV4_OCTET}\\.){3}${IPV4_OCTET}`;
/** Canonical IPv4 host address inside a /30 (e.g. 10.0.28.113/30). */
export const SLASH30_RE = new RegExp(`^${IPV4_BODY}/30$`);
/** Canonical IPv4 with a 1-2 digit prefix; range is checked by the caller. */
export const IPV4_CIDR_RE = new RegExp(`^${IPV4_BODY}/(\\d{1,2})$`);

export const Slash30Schema = z.string().regex(SLASH30_RE, 'Canonical IPv4 /30 (no leading zeros), e.g. 10.0.28.113/30');

/** IPv4, IPv6 literal or hostname; never anything with shell/RouterOS metacharacters. */
export const HostArgSchema = z.string().min(1, 'Host is required').max(253).superRefine((value, ctx) => {
  const problem = hostSyntaxError(value);
  if (problem) ctx.addIssue({ code: z.ZodIssueCode.custom, message: problem });
});

export const IdentSchema = z.string().regex(IDENT_RE, 'Letters, digits and _ . : - only (max 32)');
export const IncidentIdSchema = z.string().regex(INCIDENT_ID_RE, 'Incident ID: letters, digits, _ . - only (3-64 characters)');
export const DeviceIdSchema = z.string().min(1).max(128).regex(DEVICE_ID_RE, 'Device ID must not contain control characters');
export const WlanIfaceSchema = z.string().regex(WLAN_IFACE_RE, 'Interface name: letters, digits, _ - only (max 16)');
export const MtModeSchema = z.string().regex(MT_MODE_RE, 'Wireless mode: lowercase letters, digits and - only');
export const SsidSchema = z.string().regex(SSID_RE, 'SSID: letters, digits, space and . _ : + @ # % ( ) = , / ~ * - only (max 32)');
export const ReasonSchema = z.string().min(5).max(400)
  .regex(REASON_RE, 'Reason must be printable ASCII')
  .refine((v) => !REASON_FORBIDDEN.test(v), 'Reason must not contain ; " \' ` $ \\ | [ ]');

/** Strip control characters (audit/error text hygiene). */
export function stripControl(text: string): string {
  return text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '').replace(/[\r\n\t]+/g, ' ');
}
