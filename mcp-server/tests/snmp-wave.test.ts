import { describe, it, expect } from 'vitest';
import {
  executeWaveSnmp,
  planSnmpConfig,
  readWaveSnmpState,
  redactPlan,
  redactVerifyOutput,
  waveSnmpFilters,
  type SnmpSettings,
} from '../src/writes/snmp-config.js';

const settings: SnmpSettings = { community: 'pilotCommunity1', addresses: [], contact: 'noc-contact' };  // secret-scan:allow(test fixture: not a real community)
const fakeFix = { lat: 12.345678, lon: -98.765432 };  // secret-scan:allow(fake test coordinate)
const fakeLoc = `[${fakeFix.lat}, ${fakeFix.lon}]`;

// Shape copied from a live WaveOS `ucfg show`: SNMP lives under services.snmp.
const unconfigured = JSON.stringify({ system: { hostname: 'wave-ap' }, services: { snmp: { enabled: false } } });
const configured = (community: string) => JSON.stringify({
  system: { hostname: 'wave-ap' },
  services: { snmp: { community, contact: 'noc-contact', enabled: true, location: '[43.6, -92.8]' } },
});

describe('wave snmp', () => {
  it('reads services.snmp out of ucfg JSON', () => {
    expect(readWaveSnmpState(unconfigured)).toEqual({ enabled: false, community: null, contact: null, location: null });
    expect(readWaveSnmpState(configured('abc12345'))).toEqual({ enabled: true, community: 'abc12345', contact: 'noc-contact', location: '[43.6, -92.8]' });
    expect(() => readWaveSnmpState('BusyBox v1.31')).toThrow(/no JSON|not WaveOS JSON/);
  });

  it('plans community + the standard contact + enable, location only with a fix', () => {
    const plan = planSnmpConfig({ op: 'snmp-enable', vendor: 'wave', host: '10.0.1.9', deviceId: 'w1', reason: 'test', execute: false }, settings);
    expect(plan.valid).toBe(true);
    expect(plan.waveEdits).toEqual(waveSnmpFilters(settings.community, settings.contact));
    expect(plan.waveEdits?.join(' ')).toContain(`services.snmp.contact = ${JSON.stringify(settings.contact)}`);
    expect(plan.waveEdits?.join(' ')).not.toMatch(/services\.snmp\.location/);
    expect(plan.waveEdits).toHaveLength(3);
    // Community first, enable last: the daemon comes up already holding the identity.
    expect(plan.waveEdits?.[0]).toContain('.services.snmp.community');
    expect(plan.waveEdits?.[plan.waveEdits.length - 1]).toBe('.services.snmp.enabled = true');

    const withFix = planSnmpConfig(
      { op: 'snmp-enable', vendor: 'wave', host: '10.0.1.9', deviceId: 'w1', reason: 'test', execute: false, gps: fakeFix },
      settings,
    );
    expect(withFix.waveEdits?.join(' ')).toContain(`.services.snmp.location = ${JSON.stringify(fakeLoc)}`);

    // The planned filters carry the community, so the returned plan must not.
    const shown = JSON.stringify(redactPlan(plan, settings.community));
    expect(shown).not.toContain('pilotCommunity1');
  });

  it('applies community before enable, verifies live, then commits', async () => {
    const seen: string[] = [];
    let committed = false;
    const run = async (cmd: string): Promise<string> => {
      seen.push(cmd);
      if (cmd === 'ucfg show') return committed ? configured('pilotCommunity1') : seen.some((c) => c.includes('.services.snmp.enabled')) ? configured('pilotCommunity1') : unconfigured;
      if (cmd.startsWith("ucfg edit '.services.snmp.community")) return '';
      if (cmd.startsWith("ucfg edit '.services.snmp.contact")) return '';
      if (cmd.startsWith("ucfg edit '.services.snmp.enabled")) return '';
      if (cmd === 'ucfg commit') { committed = true; return ''; }
      throw new Error(`unexpected command ${cmd}`);
    };

    const result = await executeWaveSnmp(run, settings, planSnmpConfig({ op: 'snmp-enable', vendor: 'wave', host: '10.0.1.9', deviceId: 'w1', reason: 'test', execute: true }, settings), { postCommitDelaysMs: [0] });

    expect(result.applied).toBe(true);
    expect(result.committed).toBe(true);
    expect(result.communityChanged).toBe(true);
    expect(result.wasEnabled).toBe(false);
    expect(seen[1]).toContain('.services.snmp.community');
    expect(seen[2]).toContain('.services.snmp.contact');
    expect(seen[3]).toContain('.services.snmp.enabled');
    expect(seen).toContain('ucfg commit');
    // The verify text is returned to the agent: it must not carry the community.
    expect(result.verify).not.toContain('pilotCommunity1');
  });

  it('is a no-op on a radio already at the standard', async () => {
    const seen: string[] = [];
    const run = async (cmd: string): Promise<string> => { seen.push(cmd); return configured('pilotCommunity1'); };
    const result = await executeWaveSnmp(run, settings, planSnmpConfig({ op: 'snmp-enable', vendor: 'wave', host: '10.0.1.9', deviceId: 'w1', reason: 'test', execute: true }, settings));
    expect(result.committed).toBe(false);
    expect(seen).toEqual(['ucfg show']);
  });

  it('refuses when the live re-read does not show the change (nothing committed)', async () => {
    const run = async (cmd: string): Promise<string> => (cmd === 'ucfg show' ? unconfigured : '');
    await expect(executeWaveSnmp(run, settings, planSnmpConfig({ op: 'snmp-enable', vendor: 'wave', host: '10.0.1.9', deviceId: 'w1', reason: 'test', execute: true }, settings))).rejects.toThrow(/re-read/);
  });

  it('shows only services.snmp, never the rest of the ucfg dump', async () => {
    // The dump also carries services.uisp.key and the wireless passphrase; a
    // read-back that echoed it put a live API key in tool output.
    const dump = JSON.stringify({
      system: { hostname: 'wave-ap' },
      services: {
        snmp: { community: 'pilotCommunity1', contact: 'noc', enabled: true, location: '[1, 2]' },  // secret-scan:allow(test fixture: not a real community)
        uisp: { key: 'uisp-secret-key-value' },
        sshServer: { enabled: true },
      },
      wireless: { encryption: { passphrase: 'link-psk-value' } },
      users: [{ username: 'ubnt', password: '$6$hashhash' }],
    });
    const view = (await import('../src/writes/snmp-config.js')).waveSnmpOnlyView(dump);
    expect(view).toContain('wave-ap');
    expect(view).toContain('enabled');
    expect(view).not.toContain('pilotCommunity1');
    expect(view).not.toContain('uisp-secret-key-value');
    expect(view).not.toContain('link-psk-value');
    expect(view).not.toContain('$6$hashhash');
  });

  it('redacts every community form WaveOS prints', () => {
    const text = '{"services":{"snmp":{"community":"liveSecret99","enabled":true}}}';
    expect(redactVerifyOutput(text, 'wave')).not.toContain('liveSecret99');
    expect(redactVerifyOutput('.services.snmp.community = "liveSecret99"', 'wave')).not.toContain('liveSecret99');
  });

  it('rejects a community outside the accepted charset before building a jq filter', () => {
    expect(() => waveSnmpFilters("bad'; drop")).toThrow(/character set/);
    expect(() => waveSnmpFilters('pilotCommunity1', 'bad"; drop')).toThrow(/character set/);
  });
});
