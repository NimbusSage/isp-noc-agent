import { describe, it, expect, afterEach, vi } from 'vitest';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, rmSync, statSync, symlinkSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { planTempOspf, buildTempOspfCommands } from '../src/temp-site/ospf.js';
import {
  applyMtAlias, applyMtFlip, applyMtBounce, applyApWds, removeMtAlias, rollbackMtFlip, rollbackOspfAdjacency, applyOspfAdjacency,
  TempOspfApplySchema, TempMtFlipSchema, TempMtAliasSchema, type RouterRun,
} from '../src/temp-site/mikrotik.js';
import { planTempRadio } from '../src/temp-site/radio.js';
import {
  RouterOsWriteSchema, executeRouterOsWrite, looksLikeUplink, parsePoePauseMs, planRouterOsWrite, type RouterOsWrite,
} from '../src/writes/routeros-writes.js';
import {
  ensurePrivateDir, readSnapshotFile, snapshotDir, snapshotName, writeSnapshotFile, LEGACY_SNAPSHOT_DIR,
} from '../src/temp-site/snapshots.js';

const noop: RouterRun = async () => '';

describe('OSPF planner: positive allowlists + quoting', () => {
  const base = {
    incidentId: 'INC-77', host: '10.0.0.1', interfaze: 'ether1',
    address: '10.0.28.113/30', network: '10.0.28.112/30', ospfType: 'ptp' as const, cost: 1000, routerOsVersion: 'ros7' as const,
  };
  it('quotes interface and area in every emitted command', () => {
    const cmds = buildTempOspfCommands(TempOspfApplySchema.parse({ ...base, deviceId: 'd', execute: true }));
    expect(cmds.apply[0]).toContain('interface="ether1"');
    expect(cmds.apply[1]).toContain('interfaces="ether1"');
    expect(cmds.apply[1]).toContain('area="backbone-v2"');
    const ros6 = buildTempOspfCommands(TempOspfApplySchema.parse({ ...base, deviceId: 'd', execute: true, routerOsVersion: 'ros6' }));
    expect(ros6.apply[1]).toContain('interface="ether1"');
  });
  it('rejects spaces, newlines, brackets, quotes in interface/area (old deny-list let them through)', () => {
    for (const interfaze of ['ether1 ; /user add name=x', 'ether1\n/system reboot', 'ether[find]', 'ether1"', 'ether 1', 'a'.repeat(33), 'ether1|x', 'ether1$']) {
      expect(planTempOspf({ ...base, interfaze }).valid, JSON.stringify(interfaze)).toBe(false);
    }
    for (const ospfArea of ['backbone ; /x', 'a\nb', 'a b']) {
      expect(planTempOspf({ ...base, ospfArea }).valid, JSON.stringify(ospfArea)).toBe(false);
    }
  });
  it('rejects non-canonical /30 addresses and bad incident IDs', () => {
    expect(planTempOspf({ ...base, address: '010.0.28.113/30', network: '10.0.28.112/30' }).valid).toBe(false);
    expect(planTempOspf({ ...base, address: '10.0.28.113/30', network: '10.00.28.112/30' }).valid).toBe(false);
    for (const incidentId of ['INC 77', 'INC-77\n', 'INC[1]', 'ab', 'x'.repeat(65)]) {
      expect(planTempOspf({ ...base, incidentId }).valid, JSON.stringify(incidentId)).toBe(false);
    }
  });
  it('rejects a leading-zero or malformed executor host', () => {
    expect(TempOspfApplySchema.safeParse({ ...base, deviceId: 'd', execute: true, host: '010.0.0.1' }).success).toBe(false);
    expect(TempOspfApplySchema.safeParse({ ...base, deviceId: 'd', execute: true, host: '10.0.0.1;x' }).success).toBe(false);
  });
});

describe('rollback / tag validation', () => {
  it('rollbackOspfAdjacency only accepts the exact tag shape built from a valid incident ID', async () => {
    const seen: string[] = [];
    const run: RouterRun = async (cmd) => { seen.push(cmd); return ''; };
    await expect(rollbackOspfAdjacency(run, 'TEMP-x"; /user add name=y; ":-REMOVE-WHEN-REPAIRED', 'ros7')).rejects.toThrow(/Invalid rollback tag/);
    await expect(rollbackOspfAdjacency(run, 'TEMP-INC 1-REMOVE-WHEN-REPAIRED', 'ros7')).rejects.toThrow(/Invalid rollback tag/);
    await expect(rollbackOspfAdjacency(run, 'anything', 'ros7')).rejects.toThrow(/Invalid rollback tag/);
    expect(seen).toHaveLength(0);
    const ok = await rollbackOspfAdjacency(run, 'TEMP-INC-9-REMOVE-WHEN-REPAIRED', 'ros7');
    expect(ok.tag).toBe('TEMP-INC-9-REMOVE-WHEN-REPAIRED');
    expect(seen.length).toBeGreaterThan(0);
  });
  it('removeMtAlias validates the incident ID', async () => {
    await expect(removeMtAlias(noop, { host: '10.0.0.1', incidentId: 'bad"id' })).rejects.toThrow(/Invalid incident ID/);
    await expect(removeMtAlias(noop, { host: '10.0.0.1', incidentId: 'bad id' })).rejects.toThrow(/Invalid incident ID/);
    await expect(removeMtAlias(noop, { host: '10.0.0.1', incidentId: 'bad\n' })).rejects.toThrow(/Invalid incident ID/);
  });
  it('rollbackMtFlip validates + quotes mode/ssid/frequency/protocol/rateset', async () => {
    const seen: string[] = [];
    const run: RouterRun = async (cmd) => { seen.push(cmd); return 'mode=ap-bridge ssid="home"'; };
    for (const bad of [
      { mode: 'ap-bridge ; /user add name=x', ssid: 'home' },
      { mode: 'ap-bridge', ssid: 'home" ; /x' },
      { mode: 'ap-bridge', ssid: 'home', frequency: '2412 x' },
      { mode: 'ap-bridge', ssid: 'home', protocol: 'a\nb' },
      { mode: 'ap-bridge', ssid: 'home', rateset: '[x]' },
      { mode: 'ap-bridge', ssid: 'home', iface: 'wlan1 x' },
      { mode: 'AP', ssid: 'home' },
    ]) {
      await expect(rollbackMtFlip(run, { host: '10.0.0.1', ...bad }), JSON.stringify(bad)).rejects.toThrow(/Invalid rollback parameters/);
    }
    expect(seen).toHaveLength(0);
    await rollbackMtFlip(run, { host: '10.0.0.1', mode: 'ap-bridge', ssid: 'home', frequency: '2467' });
    expect(seen[0]).toBe('/interface wireless set wlan1 mode="ap-bridge" ssid="home" frequency="2467"');
  });
});

describe('MikroTik executors: injection + quoting', () => {
  const FLIP = { incidentId: 'INC-MT', host: '10.0.0.9', deviceId: 'r24o', iface: 'wlan1', ssid: 'targetap', execute: true as const, confirmWithinSeconds: 600 };
  it('schemas reject metacharacters in iface/ssid/interface/address', () => {
    expect(TempMtFlipSchema.safeParse({ ...FLIP, ssid: 'a";reboot;#' }).success).toBe(false);
    expect(TempMtFlipSchema.safeParse({ ...FLIP, ssid: 'a\nb' }).success).toBe(false);
    expect(TempMtFlipSchema.safeParse({ ...FLIP, ssid: '[x]' }).success).toBe(false);
    expect(TempMtFlipSchema.safeParse({ ...FLIP, iface: 'wlan1 ; x' }).success).toBe(false);
    expect(TempMtFlipSchema.safeParse({ ...FLIP, ssid: 'My Net-5G' }).success).toBe(true);
    const alias = { incidentId: 'INC-AL', host: '10.0.0.9', deviceId: 'd', address: '10.0.20.60/24', interfaze: 'bridge1', execute: true as const };
    expect(TempMtAliasSchema.safeParse(alias).success).toBe(true);
    expect(TempMtAliasSchema.safeParse({ ...alias, address: '010.0.20.60/24' }).success).toBe(false);
    expect(TempMtAliasSchema.safeParse({ ...alias, interfaze: 'bridge1 ; /x' }).success).toBe(false);
    expect(TempMtAliasSchema.safeParse({ ...alias, interfaze: 'br\nx' }).success).toBe(false);
  });
  it('alias add quotes the interface', async () => {
    const seen: string[] = [];
    let addrs = '';
    const run: RouterRun = async (cmd) => {
      seen.push(cmd);
      if (cmd === '/ip address print') return addrs;
      if (cmd.startsWith('/ip address add')) addrs = '10.0.20.60/24 TEMP-INC-AL-REMOVE-WHEN-REPAIRED';
      return '';
    };
    await applyMtAlias(run, { incidentId: 'INC-AL', host: '10.0.0.9', deviceId: 'd', address: '10.0.20.60/24', interfaze: 'bridge1', execute: true });
    expect(seen.find((c) => c.startsWith('/ip address add'))).toBe('/ip address add address=10.0.20.60/24 interface="bridge1" comment="TEMP-INC-AL-REMOVE-WHEN-REPAIRED"');
  });
  it('refuses to flip when the radio\'s current SSID cannot be restored safely', async () => {
    const seen: string[] = [];
    const fields: Record<string, string> = { mode: 'ap-bridge', ssid: 'evil" ; /user add name=x ; "', frequency: '2412', 'wireless-protocol': 'any', 'rate-set': 'default' };
    const run: RouterRun = async (cmd) => {
      seen.push(cmd);
      const m = cmd.match(/get \S+ ([a-z-]+)\]$/);
      return m ? fields[m[1]] : '';
    };
    await expect(applyMtFlip(run, FLIP)).rejects.toThrow(/cannot restore safely/);
    expect(seen.some((c) => c.includes(' set '))).toBe(false);
  });
  it('flip/wds/bounce still work with valid input and use the plain interface name', async () => {
    const seen: string[] = [];
    const run: RouterRun = async (cmd) => {
      seen.push(cmd);
      if (cmd.startsWith('/interface wireless print')) return 'name="wlan1" mode=station-bridge wds-mode=disabled';
      if (cmd.startsWith('/interface wireless monitor')) return 'status: connected-to-ess';
      return '';
    };
    const wds = await applyApWds(run, { incidentId: 'INC-W', host: '10.0.0.9', deviceId: 'd', iface: 'wlan1', wdsMode: 'dynamic', execute: true });
    expect(seen.some((c) => c === '/interface wireless set wlan1 wds-mode=dynamic')).toBe(true);
    expect(wds.previous).toBe('disabled');
    vi.useFakeTimers();
    const p = applyMtBounce(run, { incidentId: 'INC-B', host: '10.0.0.9', deviceId: 'd', iface: 'wlan1', execute: true });
    await vi.advanceTimersByTimeAsync(25000);
    expect((await p).bounced).toBe(true);
    vi.useRealTimers();
  });
  it('applyOspfAdjacency still passes with quoted interface', async () => {
    const addr: string[] = []; const ospf: string[] = [];
    const run: RouterRun = async (cmd) => {
      if (cmd.startsWith('/ip address add')) { addr.push('10.0.28.113/30 ether1'); return ''; }
      if (cmd.includes('interface-template add')) { ospf.push('cost=1000 networks=10.0.28.112/30 type=ptp'); return ''; }
      if (cmd.startsWith('/ip address print')) return addr.join('\n');
      if (cmd.includes('print where comment')) return ospf.join('\n');
      return '';
    };
    const r = await applyOspfAdjacency(run, TempOspfApplySchema.parse({
      incidentId: 'INC-9', host: '10.0.0.1', deviceId: 'r1', address: '10.0.28.113/30', network: '10.0.28.112/30',
      interfaze: 'ether1', ospfType: 'ptp', execute: true,
    }));
    expect(r.applied).toBe(true);
  });
  it('airOS radio plan rejects bad SSIDs/hosts/IDs', () => {
    const ok = { op: 'set-mode', incidentId: 'INC-7', host: '10.0.84.11', deviceId: 'a1', mode: 'managed', ssid: 'site-5w' };
    expect(planTempRadio(ok).valid).toBe(true);
    expect(planTempRadio({ ...ok, ssid: "a'b" }).valid).toBe(false);
    expect(planTempRadio({ ...ok, ssid: 'a\nb' }).valid).toBe(false);
    expect(planTempRadio({ ...ok, host: '010.0.84.11' }).valid).toBe(false);
    expect(planTempRadio({ ...ok, incidentId: 'INC 7' }).valid).toBe(false);
  });
});

describe('routeros-write: PoE cycle always restores power', () => {
  const input: RouterOsWrite = RouterOsWriteSchema.parse({
    op: 'poe-cycle', host: '10.0.1.5', deviceId: 'sw-1', interfaze: 'ether3', reason: 'hung radio power cycle after crash', execute: true,
  });
  const saved = { ...process.env };
  afterEach(() => {
    for (const k of ['NOC_WRITE_POE_CYCLE_PAUSE_MS', 'NOC_WRITE_POE_CYCLE_STRATEGY']) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
  });
  const isOn = (c: string) => c.includes('poe-out=forced-on');

  it('normal cycle: off -> dwell (in Node, not on the router) -> on -> verify', async () => {
    const calls: string[] = [];
    const sleeps: number[] = [];
    const r = await executeRouterOsWrite(async (c) => { calls.push(c); return ''; }, input, { sleep: async (ms) => { sleeps.push(ms); } });
    expect(calls[0]).toContain('poe-out=off');
    expect(isOn(calls[1])).toBe(true);
    expect(calls[2]).toContain('ethernet monitor');
    expect(calls.some((c) => c.startsWith(':delay'))).toBe(false);
    expect(sleeps).toEqual([10000]);
    expect(r.restored).toBe(true);
  });

  it('restores the port when the dwell fails or times out', async () => {
    const calls: string[] = [];
    await expect(executeRouterOsWrite(async (c) => { calls.push(c); return ''; }, input, {
      sleep: async () => { throw new Error('SSH command timed out after 30000ms'); },
    })).rejects.toThrow(/timed out/);
    expect(calls.filter(isOn)).toHaveLength(1); // power restored despite the failure
  });

  it('restores the port even when the power-off command itself timed out (it may have applied)', async () => {
    const calls: string[] = [];
    await expect(executeRouterOsWrite(async (c) => {
      calls.push(c);
      if (c.includes('poe-out=off')) throw new Error('Command timed out after 30000ms');
      return '';
    }, input, { sleep: async () => undefined })).rejects.toThrow(/timed out/);
    expect(calls.filter(isOn)).toHaveLength(1);
  });

  it('retries the restore once on a fresh session after a dropped connection', async () => {
    const first: string[] = []; const second: string[] = [];
    const run1: (c: string) => Promise<string> = async (c) => {
      first.push(c);
      if (isOn(c)) throw new Error('Not connected');
      return '';
    };
    const r = await executeRouterOsWrite(run1, input, {
      sleep: async () => undefined,
      reconnect: async () => async (c: string) => { second.push(c); return ''; },
    });
    expect(r.restored).toBe(true);
    expect(second.filter(isOn)).toHaveLength(1);
  });

  it('fails loudly (never silently) when the restore cannot be sent', async () => {
    await expect(executeRouterOsWrite(async (c) => {
      if (isOn(c)) throw new Error('boom');
      return '';
    }, input, { sleep: async () => undefined })).rejects.toThrow(/POE RESTORE FAILED[\s\S]*left powered OFF/);
  });

  it('validates the pause: integer 1000..60000, otherwise the plan is refused', () => {
    expect(parsePoePauseMs(undefined)).toEqual({ ok: true, ms: 10000 });
    expect(parsePoePauseMs('')).toEqual({ ok: true, ms: 10000 });
    expect(parsePoePauseMs('1500')).toEqual({ ok: true, ms: 1500 });
    for (const bad of ['abc', 'NaN', '999', '60001', '1e9', '-5', '10.5', '0x10', '99999999999']) {
      expect(parsePoePauseMs(bad).ok, bad).toBe(false);
    }
    process.env.NOC_WRITE_POE_CYCLE_PAUSE_MS = 'NaN';
    const plan = planRouterOsWrite(input);
    expect(plan.valid).toBe(false);
    expect(plan.error).toMatch(/NOC_WRITE_POE_CYCLE_PAUSE_MS/);
    process.env.NOC_WRITE_POE_CYCLE_PAUSE_MS = '2500';
    expect(planRouterOsWrite(input).ops[1].cmd).toBe(':delay 2500ms');
    process.env.NOC_WRITE_POE_CYCLE_PAUSE_MS = '3000';
    expect(planRouterOsWrite(input).ops[1].cmd).toBe(':delay 3s');
  });

  it('executeRouterOsWrite refuses an invalid plan before sending anything', async () => {
    process.env.NOC_WRITE_POE_CYCLE_PAUSE_MS = '1e9';
    const calls: string[] = [];
    await expect(executeRouterOsWrite(async (c) => { calls.push(c); return ''; }, input)).rejects.toThrow(/Plan invalid/);
    expect(calls).toHaveLength(0);
  });
});

describe('routeros-write: schema + uplink heuristic', () => {
  const base = { op: 'interface-disable', host: '10.0.1.5', deviceId: 'sw-1', interfaze: 'ether4', reason: 'customer requested port disable', execute: false };
  it('schema uses positive allowlists and canonical hosts', () => {
    expect(RouterOsWriteSchema.safeParse(base).success).toBe(true);
    for (const interfaze of ['ether1"; reboot', 'ether 4', 'ether4\n', 'ether[4]', 'ether4|x']) {
      expect(RouterOsWriteSchema.safeParse({ ...base, interfaze }).success, JSON.stringify(interfaze)).toBe(false);
    }
    expect(RouterOsWriteSchema.safeParse({ ...base, host: '010.0.1.5' }).success).toBe(false);
    expect(RouterOsWriteSchema.safeParse({ ...base, reason: 'line1\nline2 ok' }).success).toBe(false);
    expect(RouterOsWriteSchema.safeParse({ ...base, reason: 'drop; /system reboot' }).success).toBe(false);
  });
  it('refuses to disable uplink-looking interfaces and allows plain ports', () => {
    for (const name of ['ether1', 'ether01', 'sfp1', 'sfp-sfpplus1', 'SFP28-1', 'qsfp1', 'qsfpplus2-1', 'ether5-uplink', 'ether7-WAN', 'ether3-trunk', 'core-link', 'prod-a', 'ether6-backhaul', 'ether2-mesh']) {
      expect(looksLikeUplink(name), name).toBe(true);
      const plan = planRouterOsWrite(RouterOsWriteSchema.parse({ ...base, interfaze: name }));
      expect(plan.valid, name).toBe(false);
    }
    for (const name of ['ether4', 'ether10', 'ether12', 'ether3']) {
      expect(looksLikeUplink(name), name).toBe(false);
      expect(planRouterOsWrite(RouterOsWriteSchema.parse({ ...base, interfaze: name })).valid, name).toBe(true);
    }
    // interface-enable and PoE ops are not blocked by the heuristic
    expect(planRouterOsWrite(RouterOsWriteSchema.parse({ ...base, op: 'interface-enable', interfaze: 'sfp1' })).valid).toBe(true);
  });
});

describe('snapshot storage is private', () => {
  const dirs: string[] = [];
  const saved = process.env.TEMP_SITE_SNAPSHOT_DIR;
  afterEach(() => {
    if (saved === undefined) delete process.env.TEMP_SITE_SNAPSHOT_DIR;
    else process.env.TEMP_SITE_SNAPSHOT_DIR = saved;
    while (dirs.length) rmSync(dirs.pop() as string, { recursive: true, force: true });
  });
  const mk = () => { const d = mkdtempSync(join(tmpdir(), 'noc-snap-')); dirs.push(d); return d; };
  const mode = (p: string) => statSync(p).mode & 0o777;

  it('default is not the predictable /tmp path', () => {
    delete process.env.TEMP_SITE_SNAPSHOT_DIR;
    const dir = snapshotDir();
    expect(dir).not.toBe(LEGACY_SNAPSHOT_DIR);
    expect(dir.startsWith('/tmp/noc-temp-site-snapshots')).toBe(false);
    expect(dir.endsWith(join('state', 'temp-site-snapshots'))).toBe(true);
  });
  it('creates the directory 0700 and the file 0600', () => {
    const base = mk();
    const dir = join(base, 'nested', 'snaps');
    process.env.TEMP_SITE_SNAPSHOT_DIR = dir;
    const file = writeSnapshotFile(snapshotName('INC-1', '10.0.0.1'), 'wireless.1.ssid=x\n');
    expect(mode(dir)).toBe(0o700);
    expect(mode(file)).toBe(0o600);
    expect(readSnapshotFile(snapshotName('INC-1', '10.0.0.1'))).toBe('wireless.1.ssid=x\n');
    expect(readSnapshotFile(snapshotName('INC-2', '10.0.0.1'))).toBeNull();
  });
  it('re-enforces 0700 on a pre-existing loose directory and 0600 on an existing file', () => {
    const base = mk();
    const dir = join(base, 'loose');
    mkdirSync(dir, { mode: 0o777 });
    chmodSync(dir, 0o777);
    process.env.TEMP_SITE_SNAPSHOT_DIR = dir;
    const file = writeSnapshotFile('a.cfg', 'one');
    chmodSync(file, 0o666);
    writeSnapshotFile('a.cfg', 'two');
    expect(mode(dir)).toBe(0o700);
    expect(mode(file)).toBe(0o600);
    expect(readFileSync(file, 'utf8')).toBe('two');
  });
  it('refuses a symlinked snapshot directory', () => {
    const base = mk();
    const real = join(base, 'real');
    mkdirSync(real);
    const link = join(base, 'link');
    symlinkSync(real, link);
    expect(() => ensurePrivateDir(link)).toThrow(/not a plain directory/);
    process.env.TEMP_SITE_SNAPSHOT_DIR = link;
    expect(() => writeSnapshotFile('x.cfg', 'y')).toThrow(/not a plain directory/);
    expect(existsSync(join(real, 'x.cfg'))).toBe(false);
  });
  it('refuses to write through a symlinked snapshot file', () => {
    const base = mk();
    const dir = join(base, 'd');
    process.env.TEMP_SITE_SNAPSHOT_DIR = dir;
    writeSnapshotFile('seed.cfg', 'x');
    const target = join(base, 'target');
    symlinkSync(target, join(dir, 'evil.cfg'));
    expect(() => writeSnapshotFile('evil.cfg', 'payload')).toThrow(/symlink/);
    expect(existsSync(target)).toBe(false);
  });
});
