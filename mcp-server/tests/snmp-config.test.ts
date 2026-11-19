import { describe, it, expect } from 'vitest';
import {
  SnmpConfigSchema,
  SnmpConfig,
  planSnmpConfig,
  redactPlan,
  redactCommunity,
  redactVerifyOutput,
  readSnmpSettings,
  readSnmpSrcAddress,
  needsSrcAddressReset,
  parseCommunityAddresses,
  checkSnmpWriteGates,
  executeMikrotikSnmp,
  poolCredentialsEnabled,
  poolCredentialTries,
  VENDOR_DEVICE_TYPES,
  interleavePoolCandidates,
  COMMUNITY_REDACTED,
  MAX_COMMUNITY_ADDRESSES,
  type SnmpSettings,
} from '../src/writes/snmp-config.js';

const base: SnmpConfig = {
  op: 'snmp-enable',
  vendor: 'mikrotik',
  host: '10.0.1.5',
  deviceId: 'sw-1',
  reason: 'enable snmp before adding the device to LibreNMS',
  execute: false,
};
const settings: SnmpSettings = { community: 'nocReadOnly2026', addresses: ['10.0.0.0/8', '192.0.2.10'], contact: 'noc-contact' };  // secret-scan:allow(test fixture: not a real community)

describe('snmp-config verify redaction', () => {
  it('redacts RouterOS community names and the trap community but keeps the poller list', () => {
    const out = redactVerifyOutput(' 0 name="public" addresses=10.0.0.0/8,192.0.2.10\n   trap-community: public\n');
    expect(out).not.toContain('public');
    expect(out).toContain('name=<redacted>');
    expect(out).toContain('addresses=10.0.0.0/8,192.0.2.10');
    expect(out).toContain('trap-community: <redacted>');
  });

  it('redacts every airOS community key, not just snmp.community', () => {
    // The single-key rule printed snmp.rocommunity in clear (found on real radios).
    const out = redactVerifyOutput('snmp.status=enabled\nsnmp.community=s3cretCommunity\nsnmp.rocommunity=s3cretRo\n', 'airos');  // secret-scan:allow(test fixture: values that must be redacted)
    expect(out).toContain('snmp.status=enabled');
    expect(out).not.toContain('s3cretCommunity');
    expect(out).not.toContain('s3cretRo');
    expect(out).toContain('snmp.rocommunity=<redacted>');
  });
});

describe('snmp-config poller list', () => {
  it('accepts plain addresses and CIDRs, trimming and de-duplicating', () => {
    const r = parseCommunityAddresses(' 10.0.0.0/8 , 192.0.2.10,10.0.0.0/8 ,');
    expect(r).toEqual({ ok: true, addresses: ['10.0.0.0/8', '192.0.2.10'] });
  });

  it('treats empty/unset as no list, not as an error', () => {
    expect(parseCommunityAddresses(undefined)).toEqual({ ok: true, addresses: [] });
    expect(parseCommunityAddresses('  ')).toEqual({ ok: true, addresses: [] });
  });

  it('refuses anything that is not a canonical IPv4 address or 1-32 bit CIDR', () => {
    for (const bad of ['010.0.0.0/8', '10.0.0.0/', '10.0.0.0/33', '::1', 'poller.example.com', '10.0.0.300']) {
      const r = parseCommunityAddresses(bad);
      expect(r.ok, bad).toBe(false);
    }
  });

  it('refuses 0.0.0.0/0: "every source" must be an unset variable, not a written value', () => {
    const r = parseCommunityAddresses('0.0.0.0/0');
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toMatch(/leave the variable unset/);
  });

  it('bounds the list length', () => {
    const many = Array.from({ length: MAX_COMMUNITY_ADDRESSES + 1 }, (_, i) => `10.0.${i}.0/24`).join(',');
    expect(parseCommunityAddresses(many).ok).toBe(false);
  });
});

describe('snmp-config src-address repair', () => {
  it('reads the value from a /snmp print, with CRLF line endings', () => {
    expect(readSnmpSrcAddress('   enabled: yes\r\n src-address: 10.0.9.9\r\n')).toBe('10.0.9.9');
    expect(readSnmpSrcAddress(' src-address: ::\r\n')).toBe('::');
    expect(readSnmpSrcAddress('src-address: \r\n')).toBe('');
  });

  it('flags a configured src-address and accepts ::, blank and an absent line', () => {
    expect(needsSrcAddressReset(' src-address: 10.0.9.9')).toBe(true);
    expect(needsSrcAddressReset(' src-address: ::')).toBe(false);
    expect(needsSrcAddressReset(' src-address: ')).toBe(false);
    expect(needsSrcAddressReset('   enabled: yes')).toBe(false);
  });
});

describe('snmp-config settings', () => {
  it('reads the community and the poller list from the environment', () => {
    const r = readSnmpSettings({ NOC_SNMP_COMMUNITY: settings.community, NOC_SNMP_CONTACT: settings.contact, NOC_SNMP_COMMUNITY_ADDRESSES: '10.0.0.0/8,192.0.2.10' } as NodeJS.ProcessEnv);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.settings).toEqual(settings);
  });

  it('installs no address restriction when the poller list is unset', () => {
    const r = readSnmpSettings({ NOC_SNMP_COMMUNITY: settings.community } as NodeJS.ProcessEnv);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.settings.addresses).toEqual([]);
  });

  it('refuses an unset community, public/private, a short value and a bad poller list', () => {
    expect(readSnmpSettings({} as NodeJS.ProcessEnv).ok).toBe(false);
    expect(readSnmpSettings({ NOC_SNMP_COMMUNITY: 'public' } as NodeJS.ProcessEnv).ok).toBe(false);
    expect(readSnmpSettings({ NOC_SNMP_COMMUNITY: 'private' } as NodeJS.ProcessEnv).ok).toBe(false);  // secret-scan:allow(test fixture: a value that must be refused)
    expect(readSnmpSettings({ NOC_SNMP_COMMUNITY: 'short1' } as NodeJS.ProcessEnv).ok).toBe(false);  // secret-scan:allow(test fixture: a value that must be refused)
    expect(readSnmpSettings({ NOC_SNMP_COMMUNITY: 'nocReadOnly2026', NOC_SNMP_COMMUNITY_ADDRESSES: 'not-a-cidr' } as NodeJS.ProcessEnv).ok).toBe(false);  // secret-scan:allow(test fixture: not a real community)
  });

  it('ignores a leftover NOC_SNMP_ALLOWED_SOURCES value', () => {
    // The per-run source list was removed from the design: a stale value must not
    // block or change anything. The poller list is the org standard, not that var.
    const r = readSnmpSettings({ NOC_SNMP_COMMUNITY: 'nocReadOnly2026', NOC_SNMP_ALLOWED_SOURCES: 'garbage' } as NodeJS.ProcessEnv);  // secret-scan:allow(test fixture: not a real community)
    expect(r.ok).toBe(true);
  });

  it('redacts the community everywhere it renders', () => {
    expect(redactCommunity(`name="${settings.community}" ok`, settings.community)).toBe(`name="${COMMUNITY_REDACTED}" ok`);
    expect(redactCommunity('no secret here', undefined)).toBe('no secret here');
  });
});

describe('snmp-config planner', () => {
  it('plans the standard mikrotik config: set-or-add, enable with trap-community, src-address repair', () => {
    const plan = planSnmpConfig(SnmpConfigSchema.parse(base), settings);
    expect(plan.valid).toBe(true);
    expect(plan.probeCmd).toContain('/snmp community print detail');
    expect(plan.communitySetCmd).toBe('/snmp community set [find name="nocReadOnly2026"] security=none addresses=10.0.0.0/8,192.0.2.10');
    expect(plan.communityAddCmd).toBe('/snmp community add name="nocReadOnly2026" security=none addresses=10.0.0.0/8,192.0.2.10');
    expect(plan.enableCmd).toBe(`/snmp set enabled=yes trap-community="${settings.community}" contact="${settings.contact}"`);
    expect(plan.srcAddressCmd).toBe('/snmp set src-address=::');
    expect(plan.verifyCmd).toContain('/snmp print');
    expect(plan.note).toContain('src-address');
  });

  it('omits addresses= when no poller list is configured and says so', () => {
    const plan = planSnmpConfig(SnmpConfigSchema.parse(base), { community: settings.community, addresses: [], contact: settings.contact });
    expect(plan.communityAddCmd).toBe('/snmp community add name="nocReadOnly2026" security=none');
    expect(plan.communityAddCmd).not.toContain('addresses');
    expect(plan.note).toContain('NOC_SNMP_COMMUNITY_ADDRESSES is unset');
  });

  it('plans airOS as two allowlisted cfg edits and no reboot', () => {
    const plan = planSnmpConfig(SnmpConfigSchema.parse({ ...base, vendor: 'airos' }), settings);
    expect(plan.valid).toBe(true);
    expect(plan.edits).toEqual([
      { key: 'snmp.status', value: 'enabled' },
      { key: 'snmp.community', value: settings.community },
    ]);
    // Applied only where the line already exists (sed cannot create a key).
    expect(plan.optionalEdits).toEqual([
      { key: 'snmp.rocommunity', value: settings.community },
      { key: 'snmp.contact', value: 'noc-contact' },
    ]);
    // ...and redacted in the rendered plan like every other community value.
    const redacted = redactPlan(plan, settings.community);
    expect(redacted.edits?.[1]?.value).toBe(COMMUNITY_REDACTED);
    expect(redacted.optionalEdits?.[0]?.value).toBe(COMMUNITY_REDACTED);
    expect(plan.note).toContain('never reboots');
  });

  it('plans verify as read-only for both vendors', () => {
    const mik = planSnmpConfig(SnmpConfigSchema.parse({ ...base, op: 'snmp-verify' }));
    expect(mik.valid).toBe(true);
    expect(mik.verifyCmd).toContain('/snmp print');
    expect(mik.communityAddCmd).toBeUndefined();
    const air = planSnmpConfig(SnmpConfigSchema.parse({ ...base, op: 'snmp-verify', vendor: 'airos' }));
    // The airOS audit reads every snmp.* key so trap/contact/location gaps show up.
    expect(air.verifyCmd).toBe("grep -E '^snmp\\.' /tmp/system.cfg");
  });

  it('refuses an enable with no settings', () => {
    expect(planSnmpConfig(SnmpConfigSchema.parse(base)).valid).toBe(false);
  });

  it('redacts the community out of every command in the rendered plan', () => {
    const plan = redactPlan(planSnmpConfig(SnmpConfigSchema.parse(base), settings), settings.community);
    expect(JSON.stringify(plan)).not.toContain(settings.community);
    expect(plan.edits).toBeUndefined();
    expect(plan.communityAddCmd).toContain(COMMUNITY_REDACTED);
    expect(plan.communitySetCmd).toContain(COMMUNITY_REDACTED);
    expect(plan.enableCmd).toContain(COMMUNITY_REDACTED);
    // The poller list is not a secret and stays reviewable.
    expect(plan.communityAddCmd).toContain('addresses=10.0.0.0/8');
  });

  it('rejects a non-IPv4 host and a missing reason', () => {
    expect(SnmpConfigSchema.safeParse({ ...base, host: '10.0.1' }).success).toBe(false);
    expect(SnmpConfigSchema.safeParse({ ...base, reason: '' }).success).toBe(false);
  });
});

describe('snmp-config credential pools', () => {
  it('tries the infrastructure pool before the CPE pool, once each per round', () => {
    expect(VENDOR_DEVICE_TYPES.mikrotik).toEqual(['mikrotik-ssh', 'customer-mt']);
    expect(VENDOR_DEVICE_TYPES.airos).toEqual(['ubnt-airos', 'customer-airos']);
    expect(interleavePoolCandidates([['a1', 'a2'], ['b1']])).toEqual(['a1', 'b1', 'a2']);
    expect(interleavePoolCandidates([])).toEqual([]);
  });
});

describe('snmp-config gates', () => {
  it('blocks until kill switch + SNMP transport hold, then uses the default write scope', () => {
    const prev = process.env;
    process.env = { ...prev, NOC_WRITE_ENABLED: undefined, NOC_WRITE_SNMP_TRANSPORT: undefined, NOC_WRITE_ALLOWED_HOSTS: undefined };
    expect(checkSnmpWriteGates('10.0.1.5').ok).toBe(false);
    process.env.NOC_WRITE_ENABLED = 'true';
    expect(checkSnmpWriteGates('10.0.1.5').ok).toBe(false);
    process.env.NOC_WRITE_SNMP_TRANSPORT = 'verified';
    // No per-IP list needed for a private device the operator named.
    expect(checkSnmpWriteGates('10.0.1.5').ok).toBe(true);
    expect(checkSnmpWriteGates('8.8.8.8').ok).toBe(false);
    // An explicit list still narrows.
    process.env.NOC_WRITE_ALLOWED_HOSTS = '10.1.2.0/24';
    expect(checkSnmpWriteGates('10.0.1.5').ok).toBe(false);
    expect(checkSnmpWriteGates('10.1.2.9').ok).toBe(true);
    process.env = prev;
  });

  it('keeps the pool opt-in off by default and bounds the tries', () => {
    expect(poolCredentialsEnabled({} as NodeJS.ProcessEnv)).toBe(false);
    expect(poolCredentialsEnabled({ NOC_WRITE_SNMP_POOL_CREDENTIALS: 'true' } as NodeJS.ProcessEnv)).toBe(true);
    expect(poolCredentialTries({} as NodeJS.ProcessEnv)).toBe(3);
    expect(poolCredentialTries({ NOC_WRITE_SNMP_MAX_POOL_TRIES: '0' } as NodeJS.ProcessEnv)).toBe(1);
    expect(poolCredentialTries({ NOC_WRITE_SNMP_MAX_POOL_TRIES: '99' } as NodeJS.ProcessEnv)).toBe(5);
  });
});

describe('snmp-config RouterOS executor', () => {
  const plan = planSnmpConfig(SnmpConfigSchema.parse(base), settings);

  it('adds our community when none carries the name, then enables SNMP', async () => {
    const seen: string[] = [];
    const run = async (cmd: string) => {
      seen.push(cmd);
      if (cmd === plan.probeCmd) return ' 0 name="old" addresses=0.0.0.0/0\n src-address: ::';
      return '';
    };
    const result = await executeMikrotikSnmp(run, settings, plan);
    expect(result.applied).toBe(true);
    expect(result.communityExisted).toBe(false);
    expect(result.srcAddressReset).toBe(false);
    expect(seen).toEqual([plan.probeCmd, plan.communityAddCmd, plan.enableCmd, plan.verifyCmd]);
  });

  it('re-applies the standard to a community that already carries our name', async () => {
    const seen: string[] = [];
    const run = async (cmd: string) => {
      seen.push(cmd);
      if (cmd === plan.probeCmd) return ` 1 name="${settings.community}" addresses=0.0.0.0/0\n src-address: ::`;
      return '';
    };
    const result = await executeMikrotikSnmp(run, settings, plan);
    // The old stance (never touch an existing community) is what let a stale
    // standard survive: the tool now sets ours back to the org config.
    expect(result.communityExisted).toBe(true);
    expect(seen).toContain(plan.communitySetCmd);
    expect(seen).not.toContain(plan.communityAddCmd);
  });

  it('clears a configured src-address before anything else', async () => {
    const seen: string[] = [];
    const run = async (cmd: string) => {
      seen.push(cmd);
      if (cmd === plan.probeCmd) return ' 0 name="street9"\n src-address: 10.0.9.9';
      return '';
    };
    const result = await executeMikrotikSnmp(run, settings, plan);
    expect(result.srcAddressReset).toBe(true);
    expect(seen[1]).toBe('/snmp set src-address=::');
    expect(seen[0]).toBe(plan.probeCmd);
  });

  it('does not touch src-address when it is already :: or unset', async () => {
    for (const probe of [' 0 name="x"\n src-address: ::', ' 0 name="x"\n src-address: ']) {
      const seen: string[] = [];
      const run = async (cmd: string) => {
        seen.push(cmd);
        return cmd === plan.probeCmd ? probe : '';
      };
      const result = await executeMikrotikSnmp(run, settings, plan);
      expect(result.srcAddressReset).toBe(false);
      expect(seen).not.toContain('/snmp set src-address=::');
    }
  });

  it('throws when RouterOS prints a command failure instead of exiting non-zero', async () => {
    const run = async (cmd: string) => (cmd === plan.enableCmd ? 'syntax error (line 1)' : '');
    await expect(executeMikrotikSnmp(run, settings, plan)).rejects.toThrow(/rejected an SNMP step/);
  });
});
