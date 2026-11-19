import { describe, it, expect } from 'vitest';
import { formatSnmpLocation, planSnmpConfig, readSnmpSettings, waveSnmpFilters, type SnmpSettings } from '../src/writes/snmp-config.js';

// Operator rule: sysContact is the org standard on every device we provision, read
// from `NOC_SNMP_CONTACT` — the name identifies the organization, so it lives in the
// environment and never in a tracked file. sysLocation is this device's own GPS fix
// in the bracketed `[lat, lon]` form LibreNMS maps, supplied by the caller and never
// invented here. With no fix, an existing location is left exactly as it is: a
// provisioning sweep must not overwrite estate data it did not measure. With no
// contact configured, sysContact is left alone for the same reason.
const settings: SnmpSettings = { community: 'pilotCommunity1', addresses: [], contact: 'noc-contact' };  // secret-scan:allow(test fixture: not a real community)
const noContact: SnmpSettings = { community: 'pilotCommunity1', addresses: [] };  // secret-scan:allow(test fixture: not a real community)
const base = { op: 'snmp-enable', host: '10.0.1.5', deviceId: 'sw-1', reason: 'guard', execute: false } as const;
const fix = { lat: 12.345678, lon: -98.765432 };  // secret-scan:allow(fake coordinate fixture)
const loc = `[${fix.lat}, ${fix.lon}]`;

describe('snmp identity guard', () => {
  it('installs the configured contact on every vendor', () => {
    const mik = planSnmpConfig({ ...base, vendor: 'mikrotik' }, settings);
    expect(mik.enableCmd).toContain(`contact="${settings.contact}"`);

    const air = planSnmpConfig({ ...base, vendor: 'airos' }, settings);
    expect(air.optionalEdits).toContainEqual({ key: 'snmp.contact', value: 'noc-contact' });

    const wave = planSnmpConfig({ ...base, vendor: 'wave' }, settings);
    expect(wave.waveEdits?.join(' ')).toContain(`services.snmp.contact = ${JSON.stringify(settings.contact)}`);
  });

  it('writes no sysContact when NOC_SNMP_CONTACT is unset', () => {
    // The value is org identity and comes only from the environment; inventing a
    // default here would put the organization's name in a file the whole team and
    // the public repository can read.
    const parsed = readSnmpSettings({ NOC_SNMP_COMMUNITY: 'pilotCommunity1' } as NodeJS.ProcessEnv);  // secret-scan:allow(test fixture: not a real community)
    expect(parsed).toEqual({ ok: true, settings: { community: 'pilotCommunity1', addresses: [] } });  // secret-scan:allow(test fixture: not a real community)

    for (const vendor of ['mikrotik', 'airos', 'wave'] as const) {
      const plan = planSnmpConfig({ ...base, vendor }, noContact);
      const wrote = [plan.enableCmd, ...(plan.optionalEdits ?? []).map((e) => `${e.key}=${e.value}`), ...(plan.waveEdits ?? [])].join(' ');
      expect(wrote, vendor).not.toContain('contact');
    }
  });

  it('rejects a contact that would break the command it lands in', () => {
    const bad = readSnmpSettings({ NOC_SNMP_COMMUNITY: 'pilotCommunity1', NOC_SNMP_CONTACT: 'noc; reboot' } as NodeJS.ProcessEnv);  // secret-scan:allow(test fixture: not a real community)
    expect(bad.ok).toBe(false);
  });

  it('writes location only when a GPS fix is supplied, in [lat, lon] form', () => {
    const without = planSnmpConfig({ ...base, vendor: 'mikrotik' }, settings);
    expect(without.locationCmd).toBeUndefined();
    expect(without.location).toBeUndefined();

    const withFix = planSnmpConfig({ ...base, vendor: 'mikrotik', gps: fix }, settings);
    expect(withFix.location).toBe(loc);
    expect(withFix.locationCmd).toBe(`/snmp set location="${loc}"`);

    // The airOS line is only touched where the build already has it.
    expect(planSnmpConfig({ ...base, vendor: 'airos' }, settings).optionalEdits)
      .not.toContainEqual(expect.objectContaining({ key: 'snmp.location' }));
    expect(planSnmpConfig({ ...base, vendor: 'airos', gps: fix }, settings).optionalEdits)
      .toContainEqual({ key: 'snmp.location', value: loc });
  });

  it('refuses a location string that is not the bracketed pair', () => {
    expect(() => waveSnmpFilters('pilotCommunity1', 'noc-contact', 'Tower 4')).toThrow(/\[lat, lon\]/);  // secret-scan:allow(test fixture: not a real community)
  });

  it('formats to six decimals and never emits -0', () => {
    expect(formatSnmpLocation(fix)).toBe(loc);
    expect(formatSnmpLocation({ lat: -0.0000001, lon: 0 })).toBe('[0, 0]');  // secret-scan:allow(fake coordinate fixture: -0 must not be emitted)
  });
});
