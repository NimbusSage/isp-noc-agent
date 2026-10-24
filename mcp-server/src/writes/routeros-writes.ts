import { z } from 'zod';
import { checkHostAllowlist } from '../temp-site/allowlist.js';
import { DeviceIdSchema, IDENT_RE, IdentSchema, Ipv4Schema, ReasonSchema } from '../security/validators.js';

/**
 * General infrastructure-write executor for RouterOS hosts — the gated
 * counterpart of the read-only diagnostics path. Same security model as the
 * temp-site executors (which inspired this module):
 *
 *   1. NOC_WRITE_ENABLED=true            — master kill switch (default OFF)
 *   2. NOC_WRITE_ROUTEROS_TRANSPORT=verified — operator confirmed the exact
 *      commands' behavior on a lab/spare router first (one-time acknowledgement)
 *   3. NOC_WRITE_ALLOWED_HOSTS           — exact IPs and/or CIDR allowlist
 *   4. execute=true                      — dry-run plan by default
 *   5. exact-device credential           — a single encrypted mikrotik-ssh
 *      binding must match deviceId+host; type-wide credentials are never used
 *      for writes
 *   6. closed verb set                   — only the ops below exist; adding an
 *      op is a code review, not a prompt decision
 *   7. read-back verification            — every apply ends with a print query
 *      whose output is returned so the caller can confirm state
 *
 * Supported ops (closed set):
 *   poe-off  /interface ethernet poe set [find name=<iface>] poe-out=off
 *   poe-forced-on /interface ethernet poe set [find name=<iface>] poe-out=forced-on
 *   poe-auto      /interface ethernet poe set [find name=<iface>] poe-out=auto
 *   poe-cycle     off -> pause (NOC_WRITE_POE_CYCLE_PAUSE_MS, default 10000)
 *                 -> on (strategy forced-on unless NOC_WRITE_POE_CYCLE_STRATEGY=auto)
 *   interface-disable|interface-enable  /interface enable|disable [find name=<iface>]
 *
 * POE-cycling or disabling a PoE port cuts power to the device that hangs off
 * it — callers must know the port map before using these ops.
 *
 * Hardening (Lane 2b):
 *  - host/interface/reason are POSITIVE allowlists (see security/validators.ts).
 *  - poe-cycle restores power in a `finally`: a timeout, a dropped session or a
 *    bad pause can no longer leave the port powered off. The dwell is measured
 *    in Node (not a router-side `:delay`), so the SSH command timeout cannot
 *    interrupt it, and a failed restore is retried once on a fresh session.
 *  - NOC_WRITE_POE_CYCLE_PAUSE_MS must be an integer 1000..60000 or the plan is
 *    refused (previously NaN/huge values produced `:delay NaNs`).
 *  - interface-disable refuses anything that looks like an uplink.
 */

export const RouterOsWriteSchema = z.object({
  op: z.enum([
    'poe-off', 'poe-forced-on', 'poe-auto', 'poe-cycle',
    'interface-enable', 'interface-disable',
  ]),
  host: Ipv4Schema,
  deviceId: DeviceIdSchema,
  interfaze: IdentSchema,
  reason: ReasonSchema,
  execute: z.boolean(),
});
export type RouterOsWrite = z.infer<typeof RouterOsWriteSchema>;

export type RouterRun = (cmd: string) => Promise<string>;

export interface RouterOsWriteStep {
  cmd: string;
  why: string;
  /** 'delay' steps are executed by the caller's clock, never sent to the router. */
  kind?: 'delay';
  delayMs?: number;
}

export interface RouterOsWritePlan {
  valid: boolean;
  error?: string;
  ops: RouterOsWriteStep[];
  verifyCmd: string;
  note: string;
}

export const POE_PAUSE_MIN_MS = 1000;
export const POE_PAUSE_MAX_MS = 60000;
export const POE_PAUSE_DEFAULT_MS = 10000;

export function parsePoePauseMs(raw: string | undefined): { ok: true; ms: number } | { ok: false; error: string } {
  if (raw === undefined || raw.trim() === '') return { ok: true, ms: POE_PAUSE_DEFAULT_MS };
  const text = raw.trim();
  const ms = /^\d{1,6}$/.test(text) ? Number(text) : NaN;
  if (!Number.isFinite(ms) || ms < POE_PAUSE_MIN_MS || ms > POE_PAUSE_MAX_MS) {
    return { ok: false, error: `NOC_WRITE_POE_CYCLE_PAUSE_MS must be an integer between ${POE_PAUSE_MIN_MS} and ${POE_PAUSE_MAX_MS} (got "${text.slice(0, 20)}")` };
  }
  return { ok: true, ms };
}

const UPLINK_NAME_RE = /(uplink|wan|trunk|core|prod|backhaul|mesh)/i;
const FIBER_PORT_RE = /^q?sfp/i; // sfp, sfp-sfpplus1, sfpplus, qsfp, qsfpplus, sfp28 ...
const ETHER1_RE = /^ether0*1$/i;

/**
 * Heuristic only: names that usually carry an uplink/production path. Used to
 * refuse interface-disable; rename the port or use the operator-led path if the
 * disable is truly intended.
 */
export function looksLikeUplink(name: string): boolean {
  return UPLINK_NAME_RE.test(name) || FIBER_PORT_RE.test(name) || ETHER1_RE.test(name);
}

function delayCmd(ms: number): string {
  return ms % 1000 === 0 ? `:delay ${ms / 1000}s` : `:delay ${ms}ms`;
}

export function planRouterOsWrite(input: RouterOsWrite): RouterOsWritePlan {
  const refuse = (error: string): RouterOsWritePlan => ({ valid: false, error, ops: [], verifyCmd: '', note: '' });
  // Defense in depth for direct callers that skipped the zod schema.
  if (!IDENT_RE.test(input.interfaze)) return refuse('Interface name must use only letters, digits and _ . : - (max 32)');
  const poeSet = (mode: string) =>
    `/interface ethernet poe set [find name="${input.interfaze}"] poe-out=${mode}`;
  const verifyCmd =
    `/interface ethernet monitor "${input.interfaze}" once` +
    ` ; /interface ethernet poe print where name="${input.interfaze}"`;

  switch (input.op) {
    case 'poe-off':
    case 'poe-forced-on':
    case 'poe-auto': {
      const mode = input.op === 'poe-off' ? 'off' : input.op === 'poe-forced-on' ? 'forced-on' : 'auto';
      return {
        valid: true,
        ops: [{ cmd: poeSet(mode), why: `set POE power state ${mode} on ${input.host} ${input.interfaze}` }],
        verifyCmd,
        note: 'Read-back output follows the apply in the result; confirm poe-out= matches the requested state.',
      };
    }
    case 'poe-cycle': {
      const onMode = (process.env.NOC_WRITE_POE_CYCLE_STRATEGY || 'forced-on') === 'auto' ? 'auto' : 'forced-on';
      const pause = parsePoePauseMs(process.env.NOC_WRITE_POE_CYCLE_PAUSE_MS);
      if (!pause.ok) return refuse(pause.error);
      return {
        valid: true,
        ops: [
          { cmd: poeSet('off'), why: `power off ${input.interfaze}` },
          { cmd: delayCmd(pause.ms), why: `power-off dwell (restores power after ${pause.ms}ms)`, kind: 'delay', delayMs: pause.ms },
          { cmd: poeSet(onMode), why: `restore power (${onMode}) on ${input.interfaze}` },
        ],
        verifyCmd,
        note: 'Cutting POE power drops the device on that port. Confirm the port map first; ping-verify the device afterward.',
      };
    }
    case 'interface-enable':
    case 'interface-disable': {
      const verb = input.op === 'interface-enable' ? 'enable' : 'disable';
      if (input.op === 'interface-disable' && looksLikeUplink(input.interfaze)) {
        return refuse(`interface-disable refused for ${input.interfaze}: looks like an uplink/production port — use the operator-led path for this op`);
      }
      return {
        valid: true,
        ops: [{ cmd: `/interface ${verb} [find name="${input.interfaze}"]`, why: `${verb} interface ${input.interfaze} on ${input.host}` }],
        verifyCmd,
        note: 'Disabling a module/port carrying PoE also cuts device power; disabling an uplink isolates the site.',
      };
    }
    default:
      return refuse(`unsupported op: ${String(input.op)}`);
  }
}

export function checkInfraWriteGates(host: string): { ok: true } | { ok: false; error: string } {
  if (process.env.NOC_WRITE_ENABLED !== 'true') {
    return { ok: false, error: 'Writes disabled; require NOC_WRITE_ENABLED=true (kill switch, default off)' };
  }
  if (process.env.NOC_WRITE_ROUTEROS_TRANSPORT !== 'verified') {
    return { ok: false, error: 'RouterOS write transport not verified; confirm exact command behavior on a lab/spare router and set NOC_WRITE_ROUTEROS_TRANSPORT=verified' };
  }
  return checkHostAllowlist(host, process.env.NOC_WRITE_ALLOWED_HOSTS, 'NOC_WRITE_ALLOWED_HOSTS');
}

export interface RouterOsWriteResult {
  applied: boolean;
  output: string[];
  verify: string;
  plan: RouterOsWritePlan;
  /** poe-cycle only: true once the restore-power step succeeded. */
  restored?: boolean;
}

export interface ExecuteOptions {
  /** Dwell implementation (tests inject a fake clock). */
  sleep?: (ms: number) => Promise<void>;
  /** Re-open the session and return a fresh runner; used once when the restore step fails. */
  reconnect?: () => Promise<RouterRun>;
}

const realSleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));

export async function executeRouterOsWrite(run: RouterRun, input: RouterOsWrite, opts: ExecuteOptions = {}): Promise<RouterOsWriteResult> {
  const plan = planRouterOsWrite(input);
  if (!plan.valid) throw new Error(`Plan invalid: ${plan.error}`);
  const output: string[] = [];

  if (input.op === 'poe-cycle') {
    const [offStep, delayStep, onStep] = plan.ops;
    const sleep = opts.sleep ?? realSleep;
    let primary: unknown = null;
    let restored = false;
    try {
      // A command that times out may still have been applied on the router, so
      // the restore below ALWAYS runs once the power-off has been attempted.
      output.push(await run(offStep.cmd));
      await sleep(delayStep.delayMs ?? POE_PAUSE_DEFAULT_MS);
    } catch (e) {
      primary = e;
    } finally {
      try {
        output.push(await run(onStep.cmd));
        restored = true;
      } catch (first) {
        if (opts.reconnect) {
          try {
            const fresh = await opts.reconnect();
            output.push(await fresh(onStep.cmd));
            restored = true;
          } catch (second) {
            primary = primary ?? second;
          }
        } else {
          primary = primary ?? first;
        }
      }
    }
    if (!restored) {
      throw new Error(`POE RESTORE FAILED on ${input.host} ${input.interfaze}: the port may be left powered OFF. Restore it manually (${onStep.cmd})${primary ? `; cause: ${errText(primary)}` : ''}`);
    }
    if (primary) throw primary instanceof Error ? primary : new Error(String(primary));
    const verify = await run(plan.verifyCmd);
    return { applied: true, output, verify, plan, restored: true };
  }

  for (const step of plan.ops) {
    const out = await run(step.cmd);
    output.push(out);
  }
  const verify = await run(plan.verifyCmd);
  return { applied: true, output, verify, plan };
}
