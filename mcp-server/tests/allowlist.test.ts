import { describe, it, expect } from 'vitest';
import { CommandValidator } from '../src/commands/validator.js';

describe('CommandValidator', () => {
  const validator = new CommandValidator();

  it('allows valid MikroTik commands', () => {
    const result = validator.validate('mikrotik-ssh', 'system-resource', {});
    expect(result.valid).toBe(true);
  });

  it('allows valid UBNT commands', () => {
    const result = validator.validate('ubnt-uisp', 'device-detail', { deviceId: 'abc123' });
    expect(result.valid).toBe(true);
  });

  it('rejects unknown commands', () => {
    const result = validator.validate('mikrotik-ssh', 'format-disk', {});
    expect(result.valid).toBe(false);
    expect(result.error).toContain('not allowed');
  });

  it('rejects commands with disallowed params', () => {
    const result = validator.validate('mikrotik-ssh', 'ping', { address: '8.8.8.8', count: '1000' });
    expect(result.valid).toBe(false);
    expect(result.error).toContain('count');
  });

  it('enforces read-only constraint', () => {
    const result = validator.validate('mikrotik-ssh', 'system-reboot', {});
    expect(result.valid).toBe(false);
  });

  it('validates parameter types', () => {
    const result = validator.validate('mikrotik-ssh', 'ping', { address: 123 }); // Should be string
    expect(result.valid).toBe(false);
    expect(result.error).toContain('address');
  });

  it('rejects unknown device types', () => {
    const result = validator.validate('cisco-ios', 'show-interfaces', {});
    expect(result.valid).toBe(false);
    expect(result.error).toContain('device type');
  });

  it('lists available commands for device type', () => {
    const commands = validator.listCommands('mikrotik-ssh');
    expect(commands).toContain('system-resource');
    expect(commands).toContain('interface-list');
    expect(commands).not.toContain('system-reboot');
  });

  it('allows ppp-active for CPE IP resolution (read-only)', () => {
    const result = validator.validate('mikrotik-ssh', 'ppp-active', {});
    expect(result.valid).toBe(true);
    expect(result.command?.readOnly).toBe(true);
    expect(result.command?.template).toBe('/ppp active print');
  });

  it('isolates customer pools with the same read-only commands', () => {
    const airos = validator.validate('customer-airos', 'status', {});
    expect(airos.valid).toBe(true);
    const mt = validator.validate('customer-mt', 'ppp-active', {});
    expect(mt.valid).toBe(true);
    // Pools are separate types: infra creds never resolve for customer types
    // (resolver is keyed by deviceType string) — validator confirms the types exist.
    expect(validator.listCommands('customer-airos')).toContain('stations');
    expect(validator.listCommands('customer-mt')).toContain('system-resource');
  });
});
