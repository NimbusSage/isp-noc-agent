import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const SECRET_PW = 'Hunter2-super-secret';
let dir: string;
let createDeviceClient: ReturnType<typeof vi.fn>;
let connectImpl: () => Promise<void>;

const ENV_KEYS = ['NOC_WRITE_ENABLED', 'NOC_WRITE_ROUTEROS_TRANSPORT', 'NOC_WRITE_ALLOWED_HOSTS', 'NOC_READ_ALLOWED_HOSTS', 'TEST_PASS', 'TEMP_SITE_WRITE_ENABLED', 'TEMP_SITE_ROUTEROS_TRANSPORT', 'TEMP_SITE_ALLOWED_HOSTS', 'NOC_WRITE_POE_CYCLE_PAUSE_MS'];
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  vi.resetModules();
  dir = mkdtempSync(join(tmpdir(), 'noc-srv-'));
  for (const k of ENV_KEYS) saved[k] = process.env[k];
  process.env.TEST_PASS = 'test-passphrase';
  connectImpl = async () => undefined;
  createDeviceClient = vi.fn().mockImplementation(() => ({
    connect: () => connectImpl(),
    execute: vi.fn().mockResolvedValue({ device: '10.0.0.5', command: 'x', category: 'system', status: 'success', result: { ok: true }, warnings: [], redactions: 0, timestamp: 't', durationMs: 1 }),
    disconnect: vi.fn().mockResolvedValue(undefined),
    runRaw: vi.fn().mockResolvedValue(''),
  }));
  vi.doMock('../src/devices/factory.js', () => ({ createDeviceClient }));
  vi.doMock('../src/credentials/store.js', () => ({
    CredentialStore: vi.fn().mockImplementation(() => ({
      load: vi.fn().mockResolvedValue({
        version: 1,
        deviceTypes: [{ deviceType: 'mikrotik-ssh', credentials: [{ name: 'p', username: 'admin', password: SECRET_PW, priority: 0, tags: [] }] }],
        exactDeviceCredentials: [{ deviceType: 'mikrotik-ssh', deviceId: 'sw-1', host: '10.0.1.5', credential: { name: 'x', username: 'admin', password: SECRET_PW, priority: 0, tags: [] } }],
      }),
    })),
  }));
});
afterEach(() => {
  for (const k of ENV_KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
  vi.doUnmock('../src/devices/factory.js');
  vi.doUnmock('../src/credentials/store.js');
  rmSync(dir, { recursive: true, force: true });
});

async function makeServer(redaction = true) {
  const { NocMcpServer } = await import('../src/server.js');
  return new NocMcpServer({
    credentialStore: { path: join(dir, 'c.enc'), passphraseEnvVar: 'TEST_PASS' },
    auditLog: { path: join(dir, 'audit.log'), maxFileSizeMb: 1, maxFiles: 1 },
    limits: { maxConcurrentDevices: 1, commandTimeoutMs: 5000, maxOutputBytes: 4096 },
    redaction: { enabled: redaction, preserveInternalIps: [], preserveHostnames: [] },
  });
}
const auditLines = () => readFileSync(join(dir, 'audit.log'), 'utf8').trim().split('\n').map((l) => JSON.parse(l) as Record<string, unknown> & { metadata?: Record<string, unknown> });

describe('read-host policy at the server', () => {
  it('blocks run-diagnostics to a public host before any credential or connection', async () => {
    const server = await makeServer();
    const r = await server.handleToolCall('run-diagnostics', { host: '8.8.8.8', deviceType: 'mikrotik-ssh', command: 'system-resource' });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toMatch(/NOC_READ_ALLOWED_HOSTS/);
    expect(createDeviceClient).not.toHaveBeenCalled();
    expect(auditLines().at(-1)).toMatchObject({ outcome: 'blocked', category: 'policy' });
  });

  it('allows private hosts by default and public ones when listed', async () => {
    const server = await makeServer();
    const ok = await server.handleToolCall('run-diagnostics', { host: '10.0.0.5', deviceType: 'mikrotik-ssh', command: 'system-resource' });
    expect(ok.isError).toBeFalsy();
    process.env.NOC_READ_ALLOWED_HOSTS = '203.0.113.0/24';
    const listed = await server.handleToolCall('run-diagnostics', { host: '203.0.113.9', deviceType: 'mikrotik-ssh', command: 'system-resource' });
    expect(listed.isError).toBeFalsy();
    const nowBlocked = await server.handleToolCall('run-diagnostics', { host: '10.0.0.5', deviceType: 'mikrotik-ssh', command: 'system-resource' });
    expect(nowBlocked.isError).toBe(true);
  });

  it('applies to write tools too (read policy is checked even when write gates pass)', async () => {
    process.env.NOC_WRITE_ENABLED = 'true';
    process.env.NOC_WRITE_ROUTEROS_TRANSPORT = 'verified';
    process.env.NOC_WRITE_ALLOWED_HOSTS = '10.0.0.0/8';
    process.env.NOC_READ_ALLOWED_HOSTS = '192.168.0.0/16';
    const server = await makeServer();
    const r = await server.handleToolCall('routeros-write', { op: 'poe-off', host: '10.0.1.5', deviceId: 'sw-1', interfaze: 'ether3', reason: 'power cycle hung radio', execute: true });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toMatch(/NOC_READ_ALLOWED_HOSTS/);
    expect(createDeviceClient).not.toHaveBeenCalled();
  });
});

describe('injection through the tool-call entry point (what tool-cli uses)', () => {
  it('rejects an injected ping address and never builds a client', async () => {
    const server = await makeServer();
    const r = await server.handleToolCall('run-diagnostics', {
      host: '10.0.0.5', deviceType: 'mikrotik-ssh', command: 'ping', params: { address: '8.8.8.8 ; /user add name=x group=full', count: 3 },
    });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toMatch(/Validation error/);
    expect(createDeviceClient).not.toHaveBeenCalled();
  });

  it('rejects a malformed host through the shared schema (previously only the MCP transport checked)', async () => {
    const server = await makeServer();
    for (const host of ['10.0.0.5; reboot', '010.0.0.5', '10.0.0.5\n', '[::1]']) {
      const r = await server.handleToolCall('run-diagnostics', { host, deviceType: 'mikrotik-ssh', command: 'system-resource' });
      expect(r.isError, host).toBe(true);
      expect(r.content[0].text).toMatch(/Validation error/);
    }
    expect(createDeviceClient).not.toHaveBeenCalled();
  });

  it('rejects injected incident IDs and interfaces on temp-* tools (rollback tag injection)', async () => {
    const server = await makeServer();
    const r1 = await server.handleToolCall('temp-ospf-rollback', { incidentId: 'X"; /user add name=y; "', host: '10.0.1.5', deviceId: 'sw-1' });
    expect(r1.isError).toBe(true);
    expect(r1.content[0].text).toMatch(/Validation error/);
    const r2 = await server.handleToolCall('temp-mt-alias', { incidentId: 'INC-1', host: '10.0.1.5', deviceId: 'sw-1', address: '10.0.0.1/24', interfaze: 'br0\n/system reboot', execute: true });
    expect(r2.isError).toBe(true);
    const r3 = await server.handleToolCall('temp-mt-alias-remove', { incidentId: 'INC 1', host: '10.0.1.5', deviceId: 'sw-1' });
    expect(r3.isError).toBe(true);
    expect(createDeviceClient).not.toHaveBeenCalled();
  });

  it('unknown tools are reported without echoing control characters', async () => {
    const server = await makeServer();
    const r = await server.handleToolCall('nope\u0007', {});
    expect(r.isError).toBe(true);
    expect(r.content[0].text).not.toContain('\u0007');
  });

  it('absent execute on an apply tool is a dry run, not a validation failure', async () => {
    const server = await makeServer();
    const r = await server.handleToolCall('temp-mt-alias', { incidentId: 'INC-1', host: '10.0.1.5', deviceId: 'sw-1', address: '10.0.0.1/24' });
    expect(r.isError).toBeFalsy();
    expect(r.content[0].text).toMatch(/plan only/);
  });
});

describe('audit trail records the caller-supplied reason for blocked attempts', () => {
  it('routeros-write: schema failure, dry run, gate block, plan refusal', async () => {
    const server = await makeServer();
    const reason = 'customer says the radio is hung again';
    const base = { op: 'poe-off', host: '10.0.1.5', deviceId: 'sw-1', interfaze: 'ether3', reason };
    await server.handleToolCall('routeros-write', { ...base, execute: false });
    const gate = await server.handleToolCall('routeros-write', { ...base, execute: true });
    expect(gate.content[0].text).toMatch(/Blocked: Writes disabled/);
    process.env.NOC_WRITE_ENABLED = 'true';
    process.env.NOC_WRITE_ROUTEROS_TRANSPORT = 'verified';
    process.env.NOC_WRITE_ALLOWED_HOSTS = '10.0.0.0/8';
    const uplink = await server.handleToolCall('routeros-write', { ...base, op: 'interface-disable', interfaze: 'sfp-sfpplus1', execute: true });
    expect(uplink.isError).toBe(true);
    await server.handleToolCall('routeros-write', { ...base, reason: 'bad; reason', execute: true });
    const lines = auditLines().filter((l) => l.command === 'routeros-write' && l.outcome === 'blocked');
    expect(lines.length).toBeGreaterThanOrEqual(3);
    const withReason = lines.filter((l) => l.metadata?.callerReason === reason);
    expect(withReason.length).toBeGreaterThanOrEqual(3);
    expect(withReason.map((l) => l.metadata?.deviceId)).toContain('sw-1');
  });

  it('temp-* blocked attempts carry the incident ID', async () => {
    const server = await makeServer();
    await server.handleToolCall('temp-ospf-apply', {
      incidentId: 'INC-4242', host: '10.0.1.5', deviceId: 'sw-1', address: '10.0.28.113/30', network: '10.0.28.112/30',
      interfaze: 'ether1', ospfType: 'ptp', execute: true,
    });
    const last = auditLines().filter((l) => l.command === 'temp-ospf-apply').at(-1);
    expect(last?.outcome).toBe('blocked');
    expect(last?.metadata?.incidentId).toBe('INC-4242');
  });
});

describe('errors returned to the agent / audit log do not leak secrets', () => {
  it('scrubs the credential and passphrase from connection errors even with redaction disabled', async () => {
    process.env.NOC_WRITE_ENABLED = 'true';
    process.env.NOC_WRITE_ROUTEROS_TRANSPORT = 'verified';
    process.env.NOC_WRITE_ALLOWED_HOSTS = '10.0.0.0/8';
    connectImpl = async () => { throw new Error(`login failed for admin/${SECRET_PW}\nat 10.0.1.5 (key test-passphrase)`); };
    const server = await makeServer(false);
    const r = await server.handleToolCall('routeros-write', { op: 'poe-off', host: '10.0.1.5', deviceId: 'sw-1', interfaze: 'ether3', reason: 'power cycle hung radio', execute: true });
    expect(r.isError).toBe(true);
    const text = r.content[0].text;
    expect(text).not.toContain(SECRET_PW);
    expect(text).not.toContain('test-passphrase');
    expect(text).not.toContain('\n/');
    expect(text).toContain('[REDACTED]');
    const audit = readFileSync(join(dir, 'audit.log'), 'utf8');
    expect(audit).not.toContain(SECRET_PW);
    expect(audit).not.toContain('test-passphrase');
  });

  it('applies the standard redaction patterns to error text', async () => {
    process.env.NOC_WRITE_ENABLED = 'true';
    process.env.NOC_WRITE_ROUTEROS_TRANSPORT = 'verified';
    process.env.NOC_WRITE_ALLOWED_HOSTS = '10.0.0.0/8';
    connectImpl = async () => { throw new Error('router said: password=otherSecret99 rejected'); };
    const server = await makeServer(true);
    const r = await server.handleToolCall('routeros-write', { op: 'poe-off', host: '10.0.1.5', deviceId: 'sw-1', interfaze: 'ether3', reason: 'power cycle hung radio', execute: true });
    expect(r.content[0].text).not.toContain('otherSecret99');
  });

  it('PoE restore failure is surfaced loudly through the tool', async () => {
    process.env.NOC_WRITE_ENABLED = 'true';
    process.env.NOC_WRITE_ROUTEROS_TRANSPORT = 'verified';
    process.env.NOC_WRITE_ALLOWED_HOSTS = '10.0.0.0/8';
    process.env.NOC_WRITE_POE_CYCLE_PAUSE_MS = '1000';
    const runRaw = vi.fn().mockImplementation(async (cmd: string) => {
      if (cmd.includes('forced-on')) throw new Error('Not connected');
      return '';
    });
    createDeviceClient.mockImplementation(() => ({ connect: vi.fn().mockResolvedValue(undefined), disconnect: vi.fn().mockResolvedValue(undefined), runRaw }));
    const server = await makeServer();
    const r = await server.handleToolCall('routeros-write', { op: 'poe-cycle', host: '10.0.1.5', deviceId: 'sw-1', interfaze: 'ether3', reason: 'power cycle hung radio', execute: true });
    expect(r.isError).toBe(true);
    expect(r.content[0].text).toMatch(/POE RESTORE FAILED/);
    // restore was attempted on the original session and again after reconnecting
    expect(runRaw.mock.calls.filter((c) => String(c[0]).includes('forced-on')).length).toBe(2);
  }, 10000);
});
