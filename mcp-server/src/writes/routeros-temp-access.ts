import { z } from 'zod/v3';
import { checkWriteHost } from '../security/hosts.js';
import { DeviceIdSchema, IDENT_RE, IdentSchema, IPV4_CIDR_RE, Ipv4Schema, ReasonSchema } from '../security/validators.js';

/**
 * Gated RouterOS temporary-access executor — the store-credential counterpart
 * of the operator-led raw-SSH steps in the `reprogram-device` skill.
 *
 * A factory-reset radio answers only on its factory address (192.168.1.20) until
 * the router facing it hands out a lease or NATs it. Reaching that radio used to
 * require a human exporting SSHPASS for the router; this executor does the same
 * three changes through the encrypted exact-device binding the read path already
 * uses, with the same security model as `routeros-write`:
 *
 *   1. NOC_WRITE_ENABLED=true               — master kill switch (default OFF)
 *   2. NOC_WRITE_ROUTEROS_TRANSPORT=verified — operator validated the commands
 *   3. NOC_WRITE_ALLOWED_HOSTS              — exact IPs and/or CIDR allowlist
 *   4. execute=true                         — dry-run plan by default
 *   5. exact-device mikrotik-ssh binding    — type-wide credentials never write
 *   6. closed op set                        — adding an op is a code review
 *   7. fixed tag on every object created    — cleanup can only remove our own
 *   8. read-back verification               — every apply ends with a tagged print
 *
 * Supported ops (closed set):
 *   temp-address  /ip address add <gateway/prefix> on the interface facing the device
 *   temp-dhcp     temp-address + /ip pool + /ip dhcp-server + /ip dhcp-server network
 *                 (Wave/MLO: a factory radio has no SSH until it takes a lease)
 *   temp-nat      srcnat masquerade for the temp subnet (+ optional dstnat forward
 *                 to the factory address, for airOS which has no default route;
 *                 a relay also inserts one scoped, tagged forward accept so a
 *                 site forward-chain drop cannot shadow it)
 *   temp-ssh      /ip ssh set forwarding-enabled=yes, so a local `-L` forward
 *                 (scripts/ssh_tunnel.py) reaches the device with no NAT at all.
 *                 Left on by house policy: most routers here already have it.
 *   temp-cleanup  remove every object carrying the fixed tag, in dependency order
 *
 * Every created object carries comment="temp reprogram-device", and cleanup
 * removes ONLY objects with that comment: pre-existing entries (an older
 * leftover 192.168.1.1/24, an enabled /29 gateway) are left exactly as found.
 */

export const TEMP_ACCESS_COMMENT = 'temp reprogram-device';

const TempCidrSchema = z.string().regex(IPV4_CIDR_RE, 'Canonical IPv4/prefix (no leading zeros), e.g. 192.168.1.1/24');
const PortSchema = z.number().int().min(1).max(65535);

export const RouterOsTempAccessSchema = z.object({
  op: z.enum(['temp-address', 'temp-dhcp', 'temp-nat', 'temp-ssh', 'temp-cleanup']),
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
  /** Interface facing the reset device (required for every op except temp-cleanup). */
  interfaze: IdentSchema.optional(),
  /** temp-address: the router address to add, e.g. 192.168.1.1/24. */
  address: TempCidrSchema.optional(),
  /** temp-dhcp: pool/server name (default waveDHCP). */
  poolName: IdentSchema.optional(),
  /** temp-dhcp: the single leased address, e.g. 192.168.1.100. */
  poolRange: Ipv4Schema.optional(),
  /** temp-dhcp: the served subnet, e.g. 192.168.1.0/24. */
  network: TempCidrSchema.optional(),
  /** temp-dhcp: router address handed to the radio; also the address added on the interface. */
  gateway: Ipv4Schema.optional(),
  /** temp-nat: optional dstnat target (mgmt address reachable from the agent). */
  dstAddress: Ipv4Schema.optional(),
  dstPort: PortSchema.optional(),
  /** temp-nat: factory address/port on the reset device. */
  toAddress: Ipv4Schema.optional(),
  toPort: PortSchema.optional(),
  reason: ReasonSchema,
  execute: z.boolean().default(false),
});
export type RouterOsTempAccess = z.infer<typeof RouterOsTempAccessSchema>;

export type RouterRun = (cmd: string) => Promise<string>;

export interface TempAccessStep {
  cmd: string;
  why: string;
  /**
   * Idempotence probe: when the probe prints anything containing `needle`, the
   * step is skipped (an object created by an earlier job is reused, not duplicated).
   */
  unless?: { probe: string; needle: string };
}

export interface RouterOsTempAccessPlan {
  valid: boolean;
  error?: string;
  ops: TempAccessStep[];
  verifyCmd: string;
  /** temp-dhcp only: lease read-back the caller polls for the radio. */
  leaseCmd?: string;
  note: string;
  tag: string;
}

const TAG = TEMP_ACCESS_COMMENT;
const quoted = (value: string): string => `"${value}"`;

/**
 * RouterOS answers a refused command in the command output, not with an exit code.
 * Observed live: `/ip ssh set forwarding-enabled=yes` printed "syntax error" while
 * the call still reported applied: true. A command failure must always throw.
 */
const COMMAND_FAILURE_RE = /syntax error|bad command name|no such item|invalid value|failure:|not enough permission/i;

/** RFC1918/APIPA only: temp access never lands on a public prefix. */
export function isPrivateIPv4(ip: string): boolean {
  const parts = ip.split('.').map(Number);
  if (parts.length !== 4 || parts.some((p) => !Number.isInteger(p) || p < 0 || p > 255)) return false;
  const [a, b] = parts;
  if (a === 10) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 169 && b === 254) return true;
  return false;
}

function cidr(value: string): { ip: string; prefix: number } {
  const [ip, prefix] = value.split('/');
  return { ip, prefix: Number(prefix) };
}

/** Same subnet test for the small prefixes this executor creates (24-29). */
function sameSubnet(ip: string, networkIp: string, prefix: number): boolean {
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  const toInt = (addr: string) => addr.split('.').reduce((acc, o) => ((acc << 8) + Number(o)) >>> 0, 0);
  return (toInt(ip) & mask) === (toInt(networkIp) & mask);
}

function verifyFor(sections: string[]): string {
  // RouterOS 7 wants the filter BEFORE `print detail` (`/ip x print detail where ...`
  // fails with "bad command name where" — observed live), so keep any trailing
  // `detail` after the filter.
  return sections
    .map((s) => {
      const detail = s.endsWith(' detail');
      let base = detail ? s.slice(0, -' detail'.length) : s;
      // Cleanup passes bare sections (`/ip dhcp-server`), so add `print` when it
      // is missing: `/ip dhcp-server where ...` is "bad command name where".
      if (!base.endsWith(' print')) base = `${base} print`;
      return `${base} where comment=${quoted(TAG)}${detail ? ' detail' : ''}`;
    })
    .join(' ; ');
}

export function planRouterOsTempAccess(input: RouterOsTempAccess): RouterOsTempAccessPlan {
  const refuse = (error: string): RouterOsTempAccessPlan => ({ valid: false, error, ops: [], verifyCmd: '', note: '', tag: TAG });

  // Defense in depth for direct callers that skipped the zod schema.
  if (input.interfaze !== undefined && !IDENT_RE.test(input.interfaze)) {
    return refuse('Interface name must use only letters, digits and _ . : - (max 32)');
  }
  const iface = input.interfaze;

  switch (input.op) {
    case 'temp-address': {
      if (!iface) return refuse('temp-address needs the interface facing the reset device');
      if (!input.address) return refuse('temp-address needs address, e.g. 192.168.1.1/24');
      const { ip, prefix } = cidr(input.address);
      if (prefix < 24 || prefix > 29) return refuse('temp address must be a /24../29 (the factory device sits on a /24)');
      if (!isPrivateIPv4(ip)) return refuse('temp address must be RFC1918/APIPA (never a public prefix)');
      const addr = `${ip}/${prefix}`;
      return {
        valid: true,
        ops: [
          {
            cmd: `/ip address add address=${addr} interface=${quoted(iface)} comment=${quoted(TAG)}`,
            why: `put the router on the reset device's subnet on ${iface}`,
            unless: { probe: `/ip address print where address=${quoted(addr)}`, needle: addr },
          },
        ],
        verifyCmd: verifyFor(['/ip address print']),
        note: 'The reset radio (or Wave) answers on 192.168.1.20 only; remove this with temp-cleanup after the restore.',
        tag: TAG,
      };
    }
    case 'temp-dhcp': {
      if (!iface) return refuse('temp-dhcp needs the interface facing the reset device');
      if (!input.network) return refuse('temp-dhcp needs the served subnet, e.g. 192.168.1.0/24');
      if (!input.gateway) return refuse('temp-dhcp needs the router address handed to the radio, e.g. 192.168.1.1');
      if (!input.poolRange) return refuse('temp-dhcp needs the single pool address, e.g. 192.168.1.100');
      const { ip: netIp, prefix } = cidr(input.network);
      if (prefix < 24 || prefix > 29) return refuse('temp-dhcp subnet must be a /24../29');
      if (!isPrivateIPv4(netIp) || !isPrivateIPv4(input.gateway) || !isPrivateIPv4(input.poolRange)) {
        return refuse('temp-dhcp subnet, gateway and pool must be RFC1918/APIPA');
      }
      if (!sameSubnet(input.gateway, netIp, prefix)) return refuse('gateway is not inside the served subnet');
      if (!sameSubnet(input.poolRange, netIp, prefix)) return refuse('pool range is not inside the served subnet');
      const pool = input.poolName ?? 'waveDHCP';
      if (!IDENT_RE.test(pool)) return refuse('pool name must use only letters, digits and _ . : - (max 32)');
      const routerAddr = `${input.gateway}/${prefix}`;
      const net = `${netIp}/${prefix}`;
      return {
        valid: true,
        ops: [
          {
            cmd: `/ip address add address=${routerAddr} interface=${quoted(iface)} comment=${quoted(TAG)}`,
            why: `router-side address for the temp subnet on ${iface}`,
            unless: { probe: `/ip address print where address=${quoted(routerAddr)}`, needle: routerAddr },
          },
          {
            cmd: `/ip pool add name=${pool} ranges=${input.poolRange} comment=${quoted(TAG)}`,
            why: `single-address pool ${input.poolRange} (the house standard for waves)`,
            unless: { probe: `/ip pool print where name=${quoted(pool)}`, needle: pool },
          },
          {
            cmd: `/ip dhcp-server add address-pool=${pool} interface=${quoted(iface)} lease-time=5m name=${pool} comment=${quoted(TAG)}`,
            why: `DHCP server so the factory-reset wave can take a lease and enable SSH`,
            unless: { probe: `/ip dhcp-server print where name=${quoted(pool)}`, needle: pool },
          },
          {
            cmd: `/ip dhcp-server network add address=${net} gateway=${input.gateway} comment=${quoted(TAG)}`,
            why: `hand ${input.gateway} to the radio as its gateway`,
            unless: { probe: `/ip dhcp-server network print where address=${quoted(net)}`, needle: net },
          },
        ],
        verifyCmd: verifyFor(['/ip dhcp-server print detail', '/ip pool print', '/ip dhcp-server network print']),
        leaseCmd: '/ip dhcp-server lease print',
        note: 'A lease can take minutes. No lease after ~5 min means TFTP recovery or a needed power cycle — do not leave this running.',
        tag: TAG,
      };
    }
    case 'temp-nat': {
      if (!iface) return refuse('temp-nat needs the interface facing the reset device');
      if (!input.network) return refuse('temp-nat needs the device subnet, e.g. 192.168.1.0/24');
      const { ip: netIp, prefix } = cidr(input.network);
      if (prefix < 24 || prefix > 29) return refuse('temp-nat subnet must be a /24../29');
      if (!isPrivateIPv4(netIp)) return refuse('temp-nat subnet must be RFC1918/APIPA');
      const net = `${netIp}/${prefix}`;
      const dst = [input.dstAddress, input.dstPort, input.toAddress, input.toPort];
      const dstSet = dst.filter((v) => v !== undefined).length;
      if (dstSet !== 0 && dstSet !== 4) {
        return refuse('dstnat needs all of dstAddress, dstPort, toAddress and toPort, or none of them');
      }
      const ops: TempAccessStep[] = [
        {
          cmd: `/ip firewall nat add chain=srcnat action=masquerade dst-address=${net} out-interface=${quoted(iface)} comment=${quoted(TAG)}`,
          why: "the reset airOS device has no default route and won't answer off-subnet sources",
          unless: { probe: `/ip firewall nat print where comment=${quoted(TAG)} chain="srcnat"`, needle: 'srcnat' },
        },
      ];
      if (dstSet === 4) {
        ops.push({
          cmd:
            `/ip firewall nat add chain=dstnat action=dst-nat protocol=tcp dst-address=${input.dstAddress} dst-port=${input.dstPort}` +
            ` to-addresses=${input.toAddress} to-ports=${input.toPort} comment=${quoted(TAG)}`,
          why: `expose the reset device on ${input.dstAddress}:${input.dstPort}`,
          // Key on OUR tag plus the destination port: a pre-existing (often
          // disabled) rule pointing at the same factory address must never make
          // this look already-done, and a different dst-port is a different relay.
          unless: {
            probe: `/ip firewall nat print where comment=${quoted(TAG)} chain="dstnat" dst-port=${input.dstPort}`,
            needle: `dst-port=${input.dstPort}`,
          },
        });
        // The relayed packet is forwarded (dstnat rewrote it to the device), so it
        // crosses the forward chain. A production PPPoE router usually ends that
        // chain in drop (here: the securemikrotik policy), and then the relay is
        // created but unreachable. Accept it in front of everything, scoped to the
        // one translated destination and port, tagged so temp-cleanup takes it back
        // out. Filter rules match the POST-dstnat address/port, hence toAddress/toPort.
        ops.push({
          cmd:
            `/ip firewall filter add chain=forward action=accept protocol=tcp dst-address=${input.toAddress} dst-port=${input.toPort}` +
            ` comment=${quoted(TAG)} place-before=0`,
          why: `let the relay reach the device past the forward-chain drop, first rule so no existing policy shadows it`,
          unless: {
            probe: `/ip firewall filter print where comment=${quoted(TAG)} chain="forward" dst-port=${input.toPort}`,
            needle: `dst-port=${input.toPort}`,
          },
        });
      }
      return {
        valid: true,
        ops,
        verifyCmd: verifyFor(['/ip firewall nat print', '/ip firewall filter print']),
        note:
          'A dstnat pointing at a factory address is debris if a job dies; temp-cleanup removes it and the forward accept. ' +
          'The accept is one host/port, tagged, and sits above the site policy only until cleanup.',
        tag: TAG,
      };
    }
    case 'temp-ssh': {
      return {
        valid: true,
        ops: [
          {
            // RouterOS 7 values are no|local|remote|both (there is no `yes`).
            cmd: '/ip ssh set forwarding-enabled=both',
            why: 'a local ssh -L forward lets the agent reach the reset device through the router: no NAT, no forward accept, no interface change',
            // A settings object carries no comment, so probe the value itself.
            // RouterOS prints `forwarding-enabled: yes|no`.
            unless: { probe: '/ip ssh print', needle: 'forwarding-enabled: both' },
          },
        ],
        verifyCmd: '/ip ssh print',
        note:
          'Deliberately NOT removed by temp-cleanup: most routers here keep SSH forwarding on, so this is a ' +
          'setting, not debris. Then tunnel with scripts/ssh_tunnel.py --host <router> --local-port <p> --remote <device>:<port>.',
        tag: TAG,
      };
    }
    case 'temp-cleanup': {
      // Order matters: the DHCP server references the pool, the pool references
      // nothing, and the address is what made the subnet reachable.
      const sections = [
        '/ip dhcp-server',
        '/ip dhcp-server network',
        '/ip pool',
        '/ip firewall nat',
        '/ip firewall filter',
        '/ip address',
      ];
      return {
        valid: true,
        ops: sections.map((s) => ({
          cmd: `${s} remove [find where comment=${quoted(TAG)}]`,
          why: `remove the temporary ${s.replace('/ip ', '').replace('/interface ', '')} entries this tool created`,
        })),
        verifyCmd: verifyFor(sections),
        note:
          'Only objects carrying this tool\'s comment are removed; pre-existing entries stay exactly as found. ' +
          'temp-ssh leaves /ip ssh forwarding-enabled on by design, so it has no cleanup step.',
        tag: TAG,
      };
    }
    default:
      return refuse(`unsupported op: ${String(input.op)}`);
  }
}

export function checkTempAccessGates(host: string): { ok: true } | { ok: false; error: string } {
  if (process.env.NOC_WRITE_ENABLED !== 'true') {
    return { ok: false, error: 'Writes disabled; require NOC_WRITE_ENABLED=true (kill switch, default off)' };
  }
  if (process.env.NOC_WRITE_ROUTEROS_TRANSPORT !== 'verified') {
    return { ok: false, error: 'RouterOS write transport not verified; confirm exact command behavior on a lab/spare router and set NOC_WRITE_ROUTEROS_TRANSPORT=verified' };
  }
  return checkWriteHost(host);
}

export interface RouterOsTempAccessResult {
  applied: boolean;
  /** Steps skipped because the object already existed (idempotence probes). */
  skipped: string[];
  output: Array<{ cmd: string; result: string }>;
  verify: string;
  /** temp-dhcp only: current leases, polled by the caller for the radio. */
  lease?: string;
  plan: RouterOsTempAccessPlan;
}

export async function executeRouterOsTempAccess(run: RouterRun, input: RouterOsTempAccess): Promise<RouterOsTempAccessResult> {
  const plan = planRouterOsTempAccess(input);
  if (!plan.valid) throw new Error(`Plan invalid: ${plan.error}`);
  const output: Array<{ cmd: string; result: string }> = [];
  const skipped: string[] = [];

  for (const step of plan.ops) {
    if (step.unless) {
      const probe = await run(step.unless.probe);
      if (probe.toLowerCase().includes(step.unless.needle.toLowerCase())) {
        skipped.push(step.cmd);
        continue;
      }
    }
    const result = await run(step.cmd);
    output.push({ cmd: step.cmd, result });
    if (COMMAND_FAILURE_RE.test(result)) {
      throw new Error(`RouterOS rejected "${step.cmd}": ${result.trim()}`);
    }
  }
  const verify = await run(plan.verifyCmd);
  const lease = plan.leaseCmd ? await run(plan.leaseCmd) : undefined;
  return { applied: true, skipped, output, verify, ...(lease === undefined ? {} : { lease }), plan };
}
