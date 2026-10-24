import { describe, it, expect } from 'vitest';
import { RedactionEngine } from '../src/redaction/engine.js';

describe('RedactionEngine', () => {
  const engine = new RedactionEngine({
    enabled: true,
    preserveInternalIps: ['10.0.0.1'],
    preserveHostnames: ['noc-agent.internal'],
  });

  it('leaves private IP addresses visible (operator policy)', () => {
    const result = engine.redact('Server at 192.168.1.100 is down');
    expect(result.text).toContain('192.168.1.100');
    expect(result.count).toBe(0);
  });

  it('leaves allowlisted IPs visible', () => {
    const result = engine.redact('Analyzer at 10.0.0.1 responded');
    expect(result.text).toContain('10.0.0.1');
    expect(result.count).toBe(0);
  });

  it('leaves MAC addresses visible (operator policy)', () => {
    const result = engine.redact('Client MAC: AA:BB:CC:DD:EE:FF');
    expect(result.text).toContain('AA:BB:CC:DD:EE:FF');
    expect(result.count).toBe(0);
  });

  it('redacts serial numbers', () => {
    const result = engine.redact('Device S/N: ABC123XYZ789');
    expect(result.text).not.toContain('ABC123XYZ789');
  });

  it('redacts passwords in config output', () => {
    const result = engine.redact('password="admin123"');
    expect(result.text).not.toContain('admin123');
    expect(result.text).toContain('[REDACTED]');
  });

  it('preserves explicitly allowed hostnames', () => {
    const result = engine.redact('Connected to noc-agent.internal');
    expect(result.text).toContain('noc-agent.internal');
  });

  it('returns zero count when no redactions needed', () => {
    const result = engine.redact('Normal log output, nothing sensitive');
    expect(result.count).toBe(0);
  });

  it('does nothing when disabled', () => {
    const disabled = new RedactionEngine({ enabled: false, preserveInternalIps: [], preserveHostnames: [] });
    const result = disabled.redact('192.168.1.1 password="test"');
    expect(result.text).toBe('192.168.1.1 password="test"');
    expect(result.count).toBe(0);
  });

  it('redacts multiple occurrences of the same hostname', () => {
    const result = engine.redact('tower.example.com is down. Customers on tower.example.com are affected.');
    expect(result.text).not.toContain('tower.example.com');
    expect(result.text).toContain('[REDACTED_HOST]');
    expect(result.count).toBe(2);
  });

  it('leaves public IPs visible alongside hostnames policy (operator policy)', () => {
    const result = engine.redact('Gateway 10.0.0.1 unreachable. Next hop 10.0.0.1 also down.');
    expect(result.text).toContain('10.0.0.1');
    expect(result.count).toBe(0);
  });

  it('redacts different hostnames independently', () => {
    const result = engine.redact('server-a.example.com and server-b.example.com are both down.');
    expect(result.text).not.toContain('server-a.example.com');
    expect(result.text).not.toContain('server-b.example.com');
    expect(result.count).toBe(2);
  });

  it('preserves hostnames while redacting others in same text', () => {
    const result = engine.redact('noc-agent.internal is healthy but tower.example.com is down.');
    expect(result.text).toContain('noc-agent.internal');
    expect(result.text).not.toContain('tower.example.com');
  });
});

describe('RedactionEngine: Lane 2b secret patterns', () => {
  const engine = new RedactionEngine({ enabled: true, preserveInternalIps: [], preserveHostnames: [] });
  const redacts = (input: string, secret: string) => {
    const r = engine.redact(input);
    expect(r.text, `leaked from: ${input}`).not.toContain(secret);
    expect(r.count).toBeGreaterThan(0);
    return r.text;
  };

  it('redacts JSON-quoted secrets (plain and stringified twice)', () => {
    for (const key of ['password', 'psk', 'passphrase', 'secret', 'token', 'api_key', 'apiKey', 'wpa_psk', 'accessToken']) {
      redacts(`{"${key}":"s3cr3t-value-xyz"}`, 's3cr3t-value-xyz');
      redacts(`{"user":"a", "${key}" : "s3cr3t value with spaces"}`, 's3cr3t value');
      redacts(JSON.stringify({ result: JSON.stringify({ [key]: 's3cr3t-nested-xyz' }) }), 's3cr3t-nested-xyz');
    }
    expect(engine.redact('{"password":"x\\"y-secret"}').text).not.toContain('y-secret');
    // non-secret keys are untouched
    expect(engine.redact('{"hostname":"ap-1","mode":"managed"}').count).toBe(0);
  });

  it('redacts secret=, passphrase=, pre-shared-key=, wpa-psk and friends', () => {
    redacts('secret=hunter2hunter2', 'hunter2hunter2');
    redacts('radius secret: topsecretvalue', 'topsecretvalue');
    redacts('passphrase=correct-horse-battery', 'correct-horse-battery'); // secret-scan:allow(fake secret fixture for redaction test)
    redacts('wpa-pre-shared-key=abcdefgh12345678 wpa2-pre-shared-key=zyxwvuts87654321', 'abcdefgh12345678');
    const t = redacts('wpa2-pre-shared-key="my long phrase here" mode=dynamic-keys', 'long phrase');
    expect(t).toContain('mode=dynamic-keys');
    redacts('security-profile wpa-psk=0123456789abcdef', '0123456789abcdef');
    redacts('pre-shared-key: 0123456789abcdef0123456789abcdef', '0123456789abcdef0123456789abcdef');
  });

  it('redacts Authorization / Bearer tokens', () => {
    redacts('Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig', 'eyJhbGciOiJIUzI1NiJ9'); // secret-scan:allow(fake secret fixture for redaction test)
    redacts('authorization=Basic dXNlcjpwYXNzd29yZA==', 'dXNlcjpwYXNzd29yZA');
    redacts('{"headers":"x","note":"sent Bearer abcdef1234567890xyz upstream"}', 'abcdef1234567890xyz');
  });

  it('redacts every private key block flavor, including truncated ones', () => {
    for (const kind of ['OPENSSH', 'EC', 'RSA', 'DSA', 'ENCRYPTED', '']) {
      const label = kind ? `${kind} PRIVATE KEY` : 'PRIVATE KEY';
      const block = `-----BEGIN ${label}-----\nQUJDREVGR0hJSktMTU5PUA==\nMTIzNDU2Nzg5MA==\n-----END ${label}-----`;
      const text = redacts(`before ${JSON.stringify(block)} after`, 'QUJDREVGR0hJSktMTU5PUA');
      expect(text).toContain('[REDACTED_PRIVATE_KEY]');
      expect(text).toContain('after');
    }
    const cut = redacts('prefix -----BEGIN OPENSSH PRIVATE KEY-----\\nb3BlbnNzaC1rZXktdjEAAAAA', 'b3BlbnNzaC1rZXktdjEAAAAA'); // secret-scan:allow(fake secret fixture for redaction test)
    expect(cut).toContain('prefix');
  });

  it('redacts long ucfg-style psk lines (airOS system.cfg / Wave) inside stringified output', () => {
    const cfg = [
      'aaa.1.status=enabled',
      'aaa.1.wpa.psk=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd',
      'wpasec.psk=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz',
      'radio.1.mode=master',
      'radio.1.wpasec.psk=yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy',
      'wpasupplicant.profile.1.network.1.psk=aaaaaaaaaaaaaaaa', // secret-scan:allow(fake secret fixture for redaction test)
      'users.1.password=$1$salt$hashhashhash',
    ].join('\n');
    const stringified = JSON.stringify(cfg);
    const out = engine.redact(stringified).text;
    for (const leak of ['0123456789abcdef0123', 'zzzzzzzzzzzz', 'yyyyyyyyyyyy', 'aaaaaaaaaaaaaaaa', '$1$salt']) expect(out).not.toContain(leak);
    // (The pre-existing hostname pass rewrites dotted key names like "aaa.1.status"
    // to [REDACTED_HOST]; that is unrelated to secrets, so only assert on the values.)
    expect(out).toContain('psk=[REDACTED]');
    expect(out.match(/=\[REDACTED\]/g)?.length).toBeGreaterThanOrEqual(5);
    expect(out).toContain('=master');
  });

  it('does not double-count values the legacy patterns already redacted', () => {
    const r = engine.redact('password=abc123');
    expect(r.text).toBe('password=[REDACTED]');
    expect(r.count).toBe(1);
  });

  it('stays fast on large hostile-looking output (no catastrophic backtracking)', () => {
    const hostile = [
      'a_'.repeat(30000),
      'a.'.repeat(30000),
      'a'.repeat(120000),
      '"password'.repeat(10000),
      '-----BEGIN OPENSSH PRIVATE KEY-----'.repeat(2000), // secret-scan:allow(fake secret fixture for redaction test)
      'secret='.repeat(20000),
    ];
    for (const input of hostile) {
      const started = Date.now();
      engine.redact(input);
      expect(Date.now() - started, input.slice(0, 20)).toBeLessThan(3000);
    }
  });

  it('keeps existing behavior for the legacy patterns', () => {
    expect(engine.redact('password="admin123"').text).toContain('[REDACTED]');
    expect(engine.redact('community=public').text).toBe('community=[REDACTED]');
    expect(engine.redact('x-auth-token: abcdef').text).toBe('x-auth-token=[REDACTED]');
    expect(engine.redact('Device S/N: ABC123XYZ789').text).not.toContain('ABC123XYZ789');
  });
});
