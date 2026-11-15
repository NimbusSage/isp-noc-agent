import { describe, it, expect } from 'vitest';
import {
  RouterOsTempAccessSchema,
  TEMP_ACCESS_COMMENT,
  checkTempAccessGates,
  executeRouterOsTempAccess,
  isPrivateIPv4,
  planRouterOsTempAccess,
  type RouterOsTempAccess,
} from '../src/writes/routeros-temp-access.js';

const base = {
  host: '10.20.30.40',  // secret-scan:allow(address fixture for range/gate classification test)
  deviceId: 'test-router',
  interfaze: 'ether3',
  reason: 'factory-reset wave needs a DHCP lease to enable SSH',
  execute: true as const,
};

const parse = (extra: Record<string, unknown>): RouterOsTempAccess =>
  RouterOsTempAccessSchema.parse({ ...base, ...extra });

describe('routeros-temp-access: schema is a positive allowlist', () => {
  it('accepts the documented shapes', () => {
    expect(RouterOsTempAccessSchema.safeParse({ ...base, op: 'temp-address', address: '192.168.1.1/24' }).success).toBe(true);
    expect(RouterOsTempAccessSchema.safeParse({
      ...base, op: 'temp-dhcp', network: '192.168.1.0/24', gateway: '192.168.1.1', poolRange: '192.168.1.100', poolName: 'waveDHCP',
    }).success).toBe(true);
    expect(RouterOsTempAccessSchema.safeParse({ ...base, op: 'temp-ssh' }).success).toBe(true);
    expect(RouterOsTempAccessSchema.safeParse({ ...base, op: 'temp-cleanup' }).success).toBe(true);
    // execute defaults to false: a call without it only plans
    const noExec = RouterOsTempAccessSchema.parse({ ...base, op: 'temp-cleanup', execute: undefined });
    expect(noExec.execute).toBe(false);
  });

  it('rejects injection in every string that reaches a command', () => {
    for (const interfaze of ['ether3 ; /system reboot', 'ether3\n/system reboot', 'ether[3]', 'ether3"', 'eth er3', 'ether3|x']) {
      expect(RouterOsTempAccessSchema.safeParse({ ...base, op: 'temp-address', address: '192.168.1.1/24', interfaze }).success, JSON.stringify(interfaze)).toBe(false);
    }
    for (const poolName of ['wave"DHCP', 'wave DHCP', 'wave[1]', 'a\nb']) {
      expect(RouterOsTempAccessSchema.safeParse({
        ...base, op: 'temp-dhcp', network: '192.168.1.0/24', gateway: '192.168.1.1', poolRange: '192.168.1.100', poolName,
      }).success, JSON.stringify(poolName)).toBe(false);
    }
    for (const address of ['010.168.1.1/24', '192.168.1.1/x', '192.168.1.1', '192.168.1.1/24;reboot']) {
      expect(RouterOsTempAccessSchema.safeParse({ ...base, op: 'temp-address', address }).success, JSON.stringify(address)).toBe(false);
    }
    expect(RouterOsTempAccessSchema.safeParse({ ...base, op: 'temp-address', address: '192.168.1.1/24', host: '010.20.30.40' }).success).toBe(false);
    expect(RouterOsTempAccessSchema.safeParse({ ...base, op: 'temp-cleanup', reason: 'drop; /system reboot' }).success).toBe(false);
    expect(RouterOsTempAccessSchema.safeParse({ ...base, op: 'temp-nat', network: '192.168.1.0/24', dstPort: 0 }).success).toBe(false);
  });
});

describe('routeros-temp-access: plans are tagged, quoted and idempotent', () => {
  it('temp-address quotes the interface and tags the address', () => {
    const plan = planRouterOsTempAccess(parse({ op: 'temp-address', address: '192.168.1.1/24' }));
    expect(plan.valid).toBe(true);
    expect(plan.ops[0].cmd).toBe(`/ip address add address=192.168.1.1/24 interface="ether3" comment="${TEMP_ACCESS_COMMENT}"`);
    expect(plan.ops[0].unless?.needle).toBe('192.168.1.1/24');
    expect(plan.verifyCmd).toContain(TEMP_ACCESS_COMMENT);
  });

  it('temp-dhcp adds address, pool, server and network for the wave', () => {
    const plan = planRouterOsTempAccess(parse({
      op: 'temp-dhcp', network: '192.168.1.0/24', gateway: '192.168.1.1', poolRange: '192.168.1.100',
    }));
    expect(plan.valid).toBe(true);
    expect(plan.ops.map((o) => o.cmd)).toEqual([
      `/ip address add address=192.168.1.1/24 interface="ether3" comment="${TEMP_ACCESS_COMMENT}"`,
      `/ip pool add name=waveDHCP ranges=192.168.1.100 comment="${TEMP_ACCESS_COMMENT}"`,
      `/ip dhcp-server add address-pool=waveDHCP interface="ether3" lease-time=5m name=waveDHCP comment="${TEMP_ACCESS_COMMENT}"`,
      `/ip dhcp-server network add address=192.168.1.0/24 gateway=192.168.1.1 comment="${TEMP_ACCESS_COMMENT}"`,
    ]);
    expect(plan.leaseCmd).toBe('/ip dhcp-server lease print');
    // Every step is guarded so a second job reuses what exists instead of duplicating it.
    expect(plan.ops.every((o) => o.unless !== undefined)).toBe(true);
  });

  it('verify read-back puts the filter before `detail` (RouterOS 7 rejects "print detail where")', () => {
    // Observed live: `/ip dhcp-server print detail where comment=...` fails with
    // "bad command name where", so the cleanup verify silently read nothing.
    const plan = planRouterOsTempAccess(parse({
      op: 'temp-dhcp', network: '192.168.1.0/24', gateway: '192.168.1.1', poolRange: '192.168.1.100',
    }));
    expect(plan.verifyCmd).not.toContain('detail where');
    expect(plan.verifyCmd).toContain(`/ip dhcp-server print where comment="${TEMP_ACCESS_COMMENT}" detail`);
  });

  it('temp-nat emits masquerade, and dstnat + forward accept only when all four fields are supplied', () => {
    const masqOnly = planRouterOsTempAccess(parse({ op: 'temp-nat', network: '192.168.1.0/24' }));
    expect(masqOnly.valid).toBe(true);
    expect(masqOnly.ops).toHaveLength(1);
    expect(masqOnly.ops[0].cmd).toContain('chain=srcnat action=masquerade dst-address=192.168.1.0/24');

    const full = planRouterOsTempAccess(parse({
      op: 'temp-nat', network: '192.168.1.0/24', dstAddress: '10.20.30.50', dstPort: 2222, toAddress: '192.168.1.20', toPort: 22,  // secret-scan:allow(address fixture for range/gate classification test)
    }));
    expect(full.ops).toHaveLength(3);
    expect(full.ops[1].cmd).toBe(
      `/ip firewall nat add chain=dstnat action=dst-nat protocol=tcp dst-address=10.20.30.50 dst-port=2222 to-addresses=192.168.1.20 to-ports=22 comment="${TEMP_ACCESS_COMMENT}"`,  // secret-scan:allow(address fixture for range/gate classification test)
    );
    // A production router ends the forward chain in drop (securemikrotik), so a
    // relay alone is unreachable: the accept goes first, scoped to the translated
    // destination (filter rules see post-dstnat values) and carrying the tag.
    expect(full.ops[2].cmd).toBe(
      `/ip firewall filter add chain=forward action=accept protocol=tcp dst-address=192.168.1.20 dst-port=22 comment="${TEMP_ACCESS_COMMENT}" place-before=0`,
    );
    expect(full.ops[2].unless?.needle).toBe('dst-port=22');
    expect(full.verifyCmd).toContain('/ip firewall filter print where comment=');

    const partial = planRouterOsTempAccess(parse({ op: 'temp-nat', network: '192.168.1.0/24', dstAddress: '10.20.30.50' }));  // secret-scan:allow(address fixture for range/gate classification test)
    expect(partial.valid).toBe(false);
    expect(partial.error).toMatch(/all of dstAddress/);
  });

  it('temp-ssh enables forwarding and is deliberately left on (no cleanup step)', () => {
    const plan = planRouterOsTempAccess(parse({ op: 'temp-ssh' }));
    expect(plan.valid).toBe(true);
    // RouterOS 7 accepts no|local|remote|both; there is no `yes` (live: syntax error).
    expect(plan.ops.map((o) => o.cmd)).toEqual(['/ip ssh set forwarding-enabled=both']);
    expect(plan.ops[0].unless?.needle).toBe('forwarding-enabled: both');
    expect(plan.note).toMatch(/NOT removed/);
    const cleanup = planRouterOsTempAccess(parse({ op: 'temp-cleanup' }));
    expect(cleanup.ops.some((o) => o.cmd.startsWith('/ip ssh'))).toBe(false);
  });

  it('refuses public prefixes, wrong widths, and out-of-subnet addresses', () => {
    for (const address of ['8.8.8.1/24', '10.0.0.1/16', '10.0.0.1/30', '192.168.1.1/22']) {
      expect(planRouterOsTempAccess(parse({ op: 'temp-address', address })).valid, address).toBe(false);
    }
    expect(planRouterOsTempAccess(parse({
      op: 'temp-dhcp', network: '192.168.1.0/24', gateway: '192.168.2.1', poolRange: '192.168.1.100',
    })).valid).toBe(false);
    expect(planRouterOsTempAccess(parse({
      op: 'temp-dhcp', network: '192.168.1.0/24', gateway: '192.168.1.1', poolRange: '192.168.3.100',
    })).valid).toBe(false);
    expect(planRouterOsTempAccess(parse({ op: 'temp-address' })).valid).toBe(false);
    expect(planRouterOsTempAccess(parse({ op: 'temp-dhcp', network: '192.168.1.0/24' })).valid).toBe(false);
    expect(isPrivateIPv4('10.1.2.3')).toBe(true);
    expect(isPrivateIPv4('172.16.0.1')).toBe(true);  // secret-scan:allow(address fixture for range/gate classification test)
    expect(isPrivateIPv4('172.32.0.1')).toBe(false);  // secret-scan:allow(address fixture for range/gate classification test)
    expect(isPrivateIPv4('8.8.8.8')).toBe(false);
  });

  it('temp-cleanup removes only the tagged objects, in dependency order', () => {
    const plan = planRouterOsTempAccess(parse({ op: 'temp-cleanup' }));
    expect(plan.valid).toBe(true);
    expect(plan.ops.map((o) => o.cmd)).toEqual([
      `/ip dhcp-server remove [find where comment="${TEMP_ACCESS_COMMENT}"]`,
      `/ip dhcp-server network remove [find where comment="${TEMP_ACCESS_COMMENT}"]`,
      `/ip pool remove [find where comment="${TEMP_ACCESS_COMMENT}"]`,
      `/ip firewall nat remove [find where comment="${TEMP_ACCESS_COMMENT}"]`,
      `/ip firewall filter remove [find where comment="${TEMP_ACCESS_COMMENT}"]`,
      `/ip address remove [find where comment="${TEMP_ACCESS_COMMENT}"]`,
    ]);
    expect(plan.ops.every((o) => o.unless === undefined)).toBe(true);
    expect(plan.verifyCmd.split(' ; ')).toHaveLength(6);
    // Live bug: cleanup sections carry no `print`, so the read-back was
    // `/ip dhcp-server where ...` -> "bad command name where" and verified nothing.
    for (const part of plan.verifyCmd.split(' ; ')) expect(part).toContain(' print where comment=');
  });
});

describe('routeros-temp-access: executor', () => {
  it('skips a step whose probe already sees the object (reuse, not duplicate)', async () => {
    const seen: string[] = [];
    const run = async (cmd: string) => {
      seen.push(cmd);
      if (cmd.startsWith('/ip address print')) return '192.168.1.1/24  ether3';
      return '';
    };
    const r = await executeRouterOsTempAccess(run, parse({ op: 'temp-address', address: '192.168.1.1/24' }));
    expect(r.skipped).toHaveLength(1);
    expect(seen.some((c) => c.startsWith('/ip address add'))).toBe(false);
    expect(seen.at(-1)).toContain('where comment=');
  });

  it('applies when the probe is empty and reads back tagged state', async () => {
    const seen: string[] = [];
    const run = async (cmd: string) => {
      seen.push(cmd);
      return '';
    };
    const r = await executeRouterOsTempAccess(run, parse({
      op: 'temp-dhcp', network: '192.168.1.0/24', gateway: '192.168.1.1', poolRange: '192.168.1.100',
    }));
    expect(r.applied).toBe(true);
    expect(seen.filter((c) => c.includes(' add '))).toHaveLength(4);
    expect(seen).toContain('/ip dhcp-server lease print');
    expect(r.verify).toBeDefined();
  });

  it('throws when RouterOS refuses a command instead of reporting applied', async () => {
    // Live regression: `/ip ssh set forwarding-enabled=yes` printed "syntax error"
    // and the call still returned applied: true, so callers believed the setting
    // was on. A refused command now surfaces as an error.
    const run = async (cmd: string) => (cmd.includes(' set ') ? 'syntax error (line 1 column 32)\n' : '');
    await expect(executeRouterOsTempAccess(run, parse({ op: 'temp-ssh' })))
      .rejects.toThrow(/RouterOS rejected .*syntax error/);
  });

  it('refuses an invalid plan before sending anything', async () => {
    const seen: string[] = [];
    await expect(executeRouterOsTempAccess(async (c) => { seen.push(c); return ''; }, parse({ op: 'temp-address', address: '8.8.8.1/24' })))
      .rejects.toThrow(/Plan invalid/);
    expect(seen).toHaveLength(0);
  });
});

describe('routeros-temp-access: gates fail closed', () => {
  const keys = ['NOC_WRITE_ENABLED', 'NOC_WRITE_ROUTEROS_TRANSPORT', 'NOC_WRITE_ALLOWED_HOSTS'];
  const saved = Object.fromEntries(keys.map((k) => [k, process.env[k]]));
  const restore = () => {
    for (const k of keys) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  };

  it('requires the kill switch and the transport acknowledgement, then uses the default write scope', () => {
    try {
      delete process.env.NOC_WRITE_ENABLED;
      delete process.env.NOC_WRITE_ROUTEROS_TRANSPORT;
      delete process.env.NOC_WRITE_ALLOWED_HOSTS;
      expect(checkTempAccessGates('10.20.30.40').ok).toBe(false);  // secret-scan:allow(address fixture for range/gate classification test)
      process.env.NOC_WRITE_ENABLED = 'true';
      expect(checkTempAccessGates('10.20.30.40').ok).toBe(false);  // secret-scan:allow(address fixture for range/gate classification test)
      process.env.NOC_WRITE_ROUTEROS_TRANSPORT = 'verified';
      // Private space is the default write scope: no per-IP list needed.
      expect(checkTempAccessGates('10.20.30.40').ok).toBe(true);  // secret-scan:allow(address fixture for range/gate classification test)
      expect(checkTempAccessGates('8.8.8.8').ok).toBe(false);
      process.env.NOC_WRITE_ALLOWED_HOSTS = '10.20.0.0/16';  // secret-scan:allow(address fixture for range/gate classification test)
      expect(checkTempAccessGates('10.20.30.40').ok).toBe(true);  // secret-scan:allow(address fixture for range/gate classification test)
      expect(checkTempAccessGates('10.99.99.99').ok).toBe(false);  // secret-scan:allow(address fixture for range/gate classification test)
      process.env.NOC_WRITE_ALLOWED_HOSTS = '10.20.0.0/';  // secret-scan:allow(address fixture for range/gate classification test)
      expect(checkTempAccessGates('10.20.30.40').ok).toBe(false);  // secret-scan:allow(address fixture for range/gate classification test)
    } finally {
      restore();
    }
  });
});
