import { z } from 'zod/v3';
import { HostArgSchema, IDENT_RE, IdentSchema, IncidentIdSchema, INCIDENT_ID_RE, Slash30Schema } from '../security/validators.js';

export const TempOspfInputSchema = z.object({
  incidentId: IncidentIdSchema,
  host: HostArgSchema,
  // Interface names, areas and tags are POSITIVE allowlists (letters, digits,
  // _ . : -). They are pasted into RouterOS commands, so the old deny-list
  // (which let spaces, newlines and [ ] through) is gone.
  interfaze: IdentSchema,
  address: Slash30Schema,
  network: Slash30Schema,
  ospfArea: IdentSchema.default('backbone-v2'),
  // Interface type is REQUIRED with no default (INC-A-01: the
  // executor hard-coded type=ptp while both /30s landed on bridge1, a
  // broadcast segment — adjacency stalled at Exchange until both templates
  // were flipped to broadcast). Determine from live state BEFORE planning:
  // the facing interface is a bridge-port member (or a bridge itself) →
  // 'broadcast'; a standalone ether carrying only the temp link → 'ptp'.
  // Check with `/interface bridge port print` FIRST, never assume.
  ospfType: z.enum(['ptp', 'broadcast'], {
    errorMap: () => ({ message: 'ospfType is required: ptp (standalone ether) or broadcast (bridge/bridge-port member) — determine from /interface bridge port print' }),
  }),
  // Temp-link cost is ALWAYS 1000 (same fiber drain) or 10000 (cross-drain).
  // No other value is permitted: normal mesh costs (e.g. 10) would make the
  // temp path preferred and hijack traffic (the cost-lock review: cost 10).
  // Enforced here so plan AND apply fail closed on any other value.
  cost: z.number().int().refine((v) => v === 1000 || v === 10000, {
    message: 'Temp OSPF cost must be 1000 (same fiber drain) or 10000 (cross-drain networks); no other value is permitted',
  }).default(1000),
  routerOsVersion: z.enum(['ros6', 'ros7']).default('ros7'),
  helloInterval: z.number().int().min(1).max(60).default(1),
  deadInterval: z.number().int().min(2).max(300).default(20),
  priority: z.number().int().min(0).max(255).default(1),
  authId: z.number().int().min(0).max(255).default(1),
});
export type TempOspfInput = z.infer<typeof TempOspfInputSchema>;

function octetCheck(ip: string): boolean {
  return ip.split('/')[0].split('.').every((o) => {
    const n = Number(o);
    return Number.isInteger(n) && n >= 0 && n <= 255;
  });
}

function ipToInt(ip: string): number {
  const [a, b, c, d] = ip.split('/')[0].split('.').map(Number);
  return (((a << 24) | (b << 16) | (c << 8) | d) >>> 0);
}

export function validateTempOspf(input: TempOspfInput): { ok: true; tag: string } | { ok: false; error: string } {
  const parsed = TempOspfInputSchema.safeParse(input);
  if (!parsed.success) return { ok: false, error: parsed.error.issues.map((i) => i.message).join(', ') };
  const v = parsed.data;
  if (!octetCheck(v.address) || !octetCheck(v.network)) return { ok: false, error: 'Invalid IPv4 octets' };
  const addrInt = ipToInt(v.address);
  const netInt = ipToInt(v.network);
  if ((netInt & 3) !== 0) return { ok: false, error: 'Network is not the aligned /30 network address' };
  if ((addrInt & ~3) !== (netInt & ~3)) return { ok: false, error: 'Address is not a host within the given /30 network' };
  if ((addrInt & 3) === 0 || (addrInt & 3) === 3) return { ok: false, error: 'Address is the network or broadcast of the /30; use a usable host' };
  const first = Number(v.network.split('.')[0]);
  if (first !== 10 && !v.network.startsWith('172.') && !v.network.startsWith('192.168.')) {
    return { ok: false, error: 'Temporary OSPF /30 must be in RFC1918 private space' };
  }
  // Defense in depth: the schema is the primary gate; re-check the closed
  // character sets so direct callers cannot slip anything else through.
  if (!IDENT_RE.test(v.interfaze) || !IDENT_RE.test(v.ospfArea) || !INCIDENT_ID_RE.test(v.incidentId)) {
    return { ok: false, error: 'Interface, area and incident ID must use only letters, digits and _ . : - (incident ID: _ . -)' };
  }
  // Defense in depth: the schema refine above is the primary gate, but a
  // literal check here keeps direct callers from slipping a mesh cost through.
  if (v.cost !== 1000 && v.cost !== 10000) {
    return { ok: false, error: 'Temp OSPF cost must be 1000 (same fiber drain) or 10000 (cross-drain networks); no other value is permitted' };
  }
  return { ok: true, tag: `TEMP-${v.incidentId}-REMOVE-WHEN-REPAIRED` };
}

export function buildTempOspfCommands(input: TempOspfInput): { apply: string[]; rollback: string[]; tag: string } {
  const check = validateTempOspf(input);
  if (!check.ok) throw new Error((check as { error: string }).error);
  const tag = (check as { tag: string }).tag;
  const comment = `comment="${tag}"`;
  const apply: string[] = [
    `/ip address add address=${input.address} interface="${input.interfaze}" ${comment}`,
  ];
  // House convention (observed live, operator-confirmed):
  // per-link templates scoped with networks=, timers/priority/auth copied
  // from the router's existing PTP templates, so hellos never match beyond
  // the intended /30. The template TYPE comes from the caller's explicit
  // ospfType (never defaulted): broadcast on bridge segments, ptp only on a
  // standalone ether. Cost is 1000 same fiber network, 10000 across —
  // enforced by schema (never copy the existing template's cost: mesh costs
  // like 10 would make the temp path win).
  if (input.routerOsVersion === 'ros7') {
    apply.push(
      `/routing ospf interface-template add interfaces="${input.interfaze}" networks=${input.network} area="${input.ospfArea}" type=${input.ospfType} cost=${input.cost} hello-interval=${input.helloInterval}s dead-interval=${input.deadInterval}s priority=${input.priority} auth-id=${input.authId} ${comment}`,
    );
  } else {
    apply.push(
      `/routing ospf interface add interface="${input.interfaze}" cost=${input.cost} ${comment}`,
    );
  }
  const rollback: string[] = [
    `/ip address remove [find ${comment}]`,
    input.routerOsVersion === 'ros7'
      ? `/routing ospf interface-template remove [find ${comment}]`
      : `/routing ospf interface remove [find ${comment}]`,
  ];
  return { apply, rollback, tag };
}

export function planTempOspf(input: unknown): {
  valid: boolean;
  error?: string;
  apply?: string[];
  rollback?: string[];
  tag?: string;
  preflight?: string[];
} {
  const parsed = TempOspfInputSchema.safeParse(input);
  if (!parsed.success) {
    return { valid: false, error: parsed.error.issues.map((i) => i.message).join(', ') };
  }
  const check = validateTempOspf(parsed.data);
  if (!check.ok) return { valid: false, error: (check as { error: string }).error };
  const { apply, rollback, tag } = buildTempOspfCommands(parsed.data);
  return {
    valid: true,
    apply,
    rollback,
    tag,
    preflight: [
      `Verify ${parsed.data.host} reachable via read-only diagnostics and RouterOS version matches ${parsed.data.routerOsVersion}`,
      `Confirm interface ${parsed.data.interfaze} exists (/interface print) and has no conflicting address in ${parsed.data.network}`,
      `Confirm ${parsed.data.network} has no overlap in live routes (/ip route print) and Temp OSPF sheet reservation or operator-curated record exists`,
      `Snapshot before-state: /ip address print, /routing ospf export, wireless role/config on both ends`,
      `After apply (operator-run only until executor enabled): verify neighbor Full, expected routes, no default displacement; rollback with the tagged remove commands on any failure`,
    ],
  };
}
