/**
 * Host allowlist for write gates (TEMP_SITE_ALLOWED_HOSTS, NOC_WRITE_ALLOWED_HOSTS).
 *
 * Accepts exact IPv4 addresses AND CIDR ranges:
 *   TEMP_SITE_ALLOWED_HOSTS=10.0.3.22,10.0.0.0/8
 *
 * Rationale (operator decision): real temp work touches a
 * different site every time, so an exact-IP list means constant .env edits.
 * A /8-style entry keeps the network-scope intent without the toil. This is
 * still only ONE of two layers — every write additionally requires an
 * exact-device binding, which is provisioned per device and is what keeps
 * customer CPEs out of reach even when their addresses fall inside a
 * listed range. Scope says "which networks"; bindings say "which devices".
 *
 * Hardening (Lane 2b):
 *  - Fail closed on malformed entries: "10.0.0.0/" used to parse its prefix as
 *    Number('') === 0 (a /0 that allows EVERYTHING). Any malformed entry now
 *    throws HostListError instead of being skipped; the gate functions turn
 *    that into a blocked result with an explanatory error.
 *  - Prefixes must be 1-2 digits; /0../7 are refused unless ALLOW_BROAD_CIDR=1.
 *  - Octets are canonical: "010.1.2.3" (octal 8.1.2.3 to libc) is rejected both secret-scan:allow(documents the octal-address pitfall)
 *    as an entry and as the host being checked.
 */
import { HostListError, broadCidrAllowed, hostMatches, parseHostList, parseIPv4 } from '../security/hosts.js';

export { HostListError };

/** @throws HostListError when the allowlist contains a malformed entry. */
export function isHostAllowed(host: string, allowedCsv: string | undefined): boolean {
  const entries = parseHostList(allowedCsv, { allowIPv6: false, allowNames: false, allowBroad: broadCidrAllowed() });
  if (parseIPv4(host) === null) return false;
  return hostMatches(entries, host);
}

/** Gate-friendly wrapper: never throws; malformed config blocks with a clear error. */
export function checkHostAllowlist(
  host: string,
  csv: string | undefined,
  varName: string,
  suffix = '',
): { ok: true } | { ok: false; error: string } {
  try {
    if (isHostAllowed(host, csv)) return { ok: true };
  } catch (e) {
    return { ok: false, error: `${varName} is malformed (${e instanceof Error ? e.message : 'invalid'}); writes are blocked until it is fixed` };
  }
  return { ok: false, error: `Host ${host} is not in ${varName} (exact IP or CIDR)${suffix}` };
}
