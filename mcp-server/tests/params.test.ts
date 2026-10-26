import { describe, it, expect, vi, beforeEach } from 'vitest';
import { CommandValidator } from '../src/commands/validator.js';
import { COMMAND_REGISTRY, MIKROTIK_SSH_COMMANDS, UBNT_UISP_COMMANDS } from '../src/commands/allowlist.js';
import { renderTemplate, validateParamValue } from '../src/commands/params.js';
import { MikroTikClient } from '../src/devices/mikrotik.js';
import { UBNTUispClient } from '../src/devices/ubnt-uisp.js';

vi.mock('ssh2', () => ({ Client: vi.fn() }));

const validator = new CommandValidator();
const ping = (params: Record<string, unknown>) => validator.validate('mikrotik-ssh', 'ping', params);
const CRED = { name: 'c', username: 'u', password: 'p', priority: 0, tags: [] };

describe('command parameter validation (injection)', () => {
  it('rejects the audit injection strings in address', () => {
    for (const address of [
      '8.8.8.8 ; /user add name=x group=full',
      '8.8.8.8\n/user add name=x',
      '8.8.8.8\r/system reboot',
      '[/user add name=x group=full]',
      '8.8.8.8 count=1',
      '8.8.8.8|reboot',
      '$var',
      '"8.8.8.8"',
      "8.8.8.8'",
      '8.8.8.8`id`',
      '8.8.8.8\\',
      '8.8.8.8\u0000',
      '',
      'a'.repeat(300),
    ]) {
      const r = ping({ address });
      expect(r.valid, JSON.stringify(address)).toBe(false);
    }
  });

  it('accepts normal IPv4, IPv6 and hostname addresses', () => {
    for (const address of ['8.8.8.8', '10.0.0.1', '2001:db8::1', 'fe80::1', 'router-1.example.com', 'localhost']) {
      expect(ping({ address }).valid, address).toBe(true);
    }
  });

  it('rejects non-canonical IPv4 addresses (leading zeros are octal to libc)', () => {
    for (const address of ['010.1.2.3', '8.8.8', '256.1.1.1', '1.2.3.4.5', '0x8.8.8.8']) {
      expect(ping({ address }).valid, address).toBe(false);
    }
  });

  it('count must be an integer 1..20 (rejects NaN, Infinity, 1e9, floats, strings)', () => {
    for (const count of [Number.NaN, Number.POSITIVE_INFINITY, 1e9, 21, 0, -1, 1.5, '3', null, true]) {
      expect(ping({ address: '8.8.8.8', count }).valid, String(count)).toBe(false);
    }
    for (const count of [1, 3, 20]) expect(ping({ address: '8.8.8.8', count }).valid).toBe(true);
  });

  it('interface must match ^[A-Za-z0-9_.:-]{1,32}$', () => {
    for (const interfaceName of ['ether1', 'sfp-sfpplus1', 'vlan.100', 'bridge:1', 'a'.repeat(32)]) {
      expect(validator.validate('mikrotik-ssh', 'interface-monitor', { interface: interfaceName }).valid, interfaceName).toBe(true);
    }
    for (const interfaceName of ['ether1]; /user add', 'ether 1', 'ether1\n', 'a'.repeat(33), '<pppoe-x>', 'ether1"', '']) {
      expect(validator.validate('mikrotik-ssh', 'interface-monitor', { interface: interfaceName }).valid, JSON.stringify(interfaceName)).toBe(false);
    }
  });

  it('bandwidth-test: protocol/direction are enums, duration a bounded integer, user/password restricted', () => {
    const ok = { address: '10.0.0.2', user: 'admin', password: 'S3cret!pass', protocol: 'tcp', direction: 'both', duration: 10 };
    expect(validator.validate('mikrotik-ssh', 'bandwidth-test', ok).valid).toBe(true);
    expect(validator.validate('mikrotik-ssh', 'bandwidth-test', { ...ok, protocol: 'tcp ; /x' }).valid).toBe(false);
    expect(validator.validate('mikrotik-ssh', 'bandwidth-test', { ...ok, protocol: 'icmp' }).valid).toBe(false);
    expect(validator.validate('mikrotik-ssh', 'bandwidth-test', { ...ok, direction: 'sideways' }).valid).toBe(false);
    expect(validator.validate('mikrotik-ssh', 'bandwidth-test', { ...ok, duration: 3600 }).valid).toBe(false);
    expect(validator.validate('mikrotik-ssh', 'bandwidth-test', { ...ok, duration: Number.NaN }).valid).toBe(false);
    expect(validator.validate('mikrotik-ssh', 'bandwidth-test', { ...ok, user: 'admin ; /user add' }).valid).toBe(false);
    for (const password of ['pa ss', 'pa;ss', 'p$a', 'p"a', "p'a", 'p[a]', 'p\\a', 'p|a', 'p\na', 'p`a', 'p(a)']) {
      expect(validator.validate('mikrotik-ssh', 'bandwidth-test', { ...ok, password }).valid, JSON.stringify(password)).toBe(false);
    }
  });

  it('rejects unknown params and prototype-key tricks', () => {
    expect(ping({ address: '8.8.8.8', extra: 'x' }).valid).toBe(false);
    expect(validator.validate('mikrotik-ssh', 'ping', JSON.parse('{"address":"8.8.8.8","__proto__":"x"}')).valid).toBe(false);
    expect(validator.validate('mikrotik-ssh', 'ping', { address: '8.8.8.8', constructor: 'x' }).valid).toBe(false);
    expect(validator.validate('constructor', 'ping', {}).valid).toBe(false);
    expect(validator.validate('mikrotik-ssh', 'toString', {}).valid).toBe(false);
    expect(validator.getCommand('mikrotik-ssh', '__proto__')).toBeNull();
  });

  it('requires required params even when others are valid', () => {
    expect(ping({}).valid).toBe(false);
    expect(ping({ count: 3 }).valid).toBe(false);
  });

  it('UISP deviceId: UUID/alphanumeric only, no traversal', () => {
    for (const deviceId of ['abc123', '6f2d6a52-1b1f-4c1e-9a3a-0b6c2f9d7e11', 'A_b-1']) { // secret-scan:allow(fake device id fixture for parameter validation test)
      expect(validator.validate('ubnt-uisp', 'device-detail', { deviceId }).valid, deviceId).toBe(true);
    }
    for (const deviceId of ['../../etc/passwd', '..', 'a/b', 'a?x=1', 'a#b', 'a b', '%2e%2e', 'a\n', '', '-abc', 'a'.repeat(129)]) {
      expect(validator.validate('ubnt-uisp', 'device-detail', { deviceId }).valid, JSON.stringify(deviceId)).toBe(false);
    }
    expect(validator.validate('ubnt-uisp', 'device-statistics', { deviceId: 'abc', interval: 'day' }).valid).toBe(true);
    expect(validator.validate('ubnt-uisp', 'device-statistics', { deviceId: 'abc', interval: 'day&x=1' }).valid).toBe(false);
  });

  it('UISP requests carry the query parameters the API requires (checked against a live instance)', () => {
    const render = (name: string, params: Record<string, unknown>): string =>
      renderTemplate(UBNT_UISP_COMMANDS[name], validator.validate('ubnt-uisp', name, params).params ?? params, encodeURIComponent);
    // interval is mandatory and lower-case; the old default HOUR and the missing parameter both got HTTP 400.
    expect(render('device-statistics', { deviceId: 'abc' })).toBe('/nms/api/v2.1/devices/abc/statistics?interval=hour');
    expect(render('device-statistics', { deviceId: 'abc', interval: 'month' })).toContain('interval=month');
    expect(validator.validate('ubnt-uisp', 'device-statistics', { deviceId: 'abc', interval: 'HOUR' }).valid).toBe(false);
    expect(validator.validate('ubnt-uisp', 'device-statistics', { deviceId: 'abc', interval: 'range' }).valid).toBe(false);
    // outages and logs need count and page; /alerts does not exist on UISP (404).
    expect(render('outages', {})).toBe('/nms/api/v2.1/outages?count=20&page=1');
    expect(render('logs', { count: 5, page: 2 })).toBe('/nms/api/v2.1/logs?count=5&page=2');
    expect(UBNT_UISP_COMMANDS['alerts']).toBeUndefined();
    for (const bad of [{ count: 0 }, { count: 101 }, { count: 1.5 }, { count: '5;x' }, { page: 0 }]) {
      expect(validator.validate('ubnt-uisp', 'outages', bad).valid, JSON.stringify(bad)).toBe(false);
    }
  });

  it('every string param in the registry has a validation schema and every number a range', () => {
    for (const [type, commands] of Object.entries(COMMAND_REGISTRY)) {
      for (const [name, cmd] of Object.entries(commands)) {
        for (const [key, spec] of Object.entries(cmd.params)) {
          if (spec.type === 'string') expect(Boolean(spec.pattern || spec.enum || spec.check), `${type}/${name}/${key}`).toBe(true);
          if (spec.type === 'number') expect(spec.min !== undefined && spec.max !== undefined, `${type}/${name}/${key}`).toBe(true);
          if (spec.default !== undefined) expect(validateParamValue(key, spec, spec.default), `${type}/${name}/${key} default`).toBeNull();
        }
      }
    }
  });

  it('a string param with no schema is refused (fail closed)', () => {
    expect(validateParamValue('x', { type: 'string', required: true }, 'abc')).toMatch(/no validation schema/);
  });
});

describe('defense in depth: clients re-validate when the validator is bypassed', () => {
  beforeEach(() => vi.clearAllMocks());

  it('MikroTikClient.buildCommand throws on an injected address instead of pasting it', () => {
    const client = new MikroTikClient('10.0.0.1', 22, CRED);
    expect(() => client.buildCommand(MIKROTIK_SSH_COMMANDS['ping'], { address: '8.8.8.8 ; /user add name=x group=full' })).toThrow(/address/);
    expect(() => client.buildCommand(MIKROTIK_SSH_COMMANDS['ping'], { address: '8.8.8.8', count: 1e9 })).toThrow(/count/);
    expect(client.buildCommand(MIKROTIK_SSH_COMMANDS['ping'], { address: '8.8.8.8' })).toBe('/tool ping address=8.8.8.8 count=3');
  });

  it('MikroTikClient.execute returns an error result (no connection attempt output) for bad params', async () => {
    const client = new MikroTikClient('10.0.0.1', 22, CRED);
    (client as unknown as { conn: object }).conn = {};
    const r = await client.execute(MIKROTIK_SSH_COMMANDS['ping'], { address: 'x;y' });
    expect(r.status).toBe('error');
    expect(r.result).toBeNull();
  });

  it('renderTemplate fails on a missing required param and on unresolved placeholders', () => {
    expect(() => renderTemplate(MIKROTIK_SSH_COMMANDS['ping'], {})).toThrow(/Missing required parameter 'address'/);
    expect(() => renderTemplate({ ...MIKROTIK_SSH_COMMANDS['system-resource'], template: '/x {{nope}}' }, {})).toThrow(/Unresolved placeholder/);
  });

  it('UISP client percent-encodes ids and never builds a traversal URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal('fetch', fetchMock);
    const client = new UBNTUispClient('10.0.0.2', 443, { ...CRED, apiToken: 't' });
    const bad = await client.execute(UBNT_UISP_COMMANDS['device-detail'], { deviceId: '../../etc/passwd' });
    expect(bad.status).toBe('error');
    expect(fetchMock).not.toHaveBeenCalled();
    const good = await client.execute(UBNT_UISP_COMMANDS['device-detail'], { deviceId: 'abc-123' });
    expect(good.status).toBe('success');
    expect(fetchMock.mock.calls[0][0]).toBe('https://10.0.0.2:443/nms/api/v2.1/devices/abc-123/detail');
    // encodeURIComponent is applied even if a looser schema were ever configured.
    const loose = { ...UBNT_UISP_COMMANDS['device-detail'], params: { deviceId: { type: 'string' as const, required: true, pattern: /^.+$/ } } };
    expect(renderTemplate(loose, { deviceId: 'a b%c' }, encodeURIComponent)).toBe('/nms/api/v2.1/devices/a%20b%25c/detail');
    vi.unstubAllGlobals();
  });
});
