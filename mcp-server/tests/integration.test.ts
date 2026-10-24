import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdirSync, rmSync } from 'fs';
import { join } from 'path';

const TEST_DIR = '/tmp/noc-mcp-test-integration';

describe('NocMcpServer', () => {
  it('lists available tools', async () => {
    const { NocMcpServer } = await import('../src/server.js');
    const server = new NocMcpServer({
      credentialStore: { path: '', passphraseEnvVar: 'TEST' },
      auditLog: { path: '/tmp/test-audit.log', maxFileSizeMb: 1, maxFiles: 1 },
      limits: { maxConcurrentDevices: 1, commandTimeoutMs: 5000, maxOutputBytes: 1024 },
      redaction: { enabled: true, preserveInternalIps: [], preserveHostnames: [] },
    });

    const tools = server.getToolDefinitions();
    expect(tools).toHaveLength(17);
    expect(tools[0].name).toBe('run-diagnostics');
    expect(tools[0].inputSchema.required).toEqual(['host', 'deviceType', 'command']);
    expect(Object.keys(tools[0].inputSchema.properties)).toEqual(['host', 'deviceType', 'command', 'params']);
    expect(tools[1].name).toBe('run-exact-device-diagnostics');
    expect(tools[1].inputSchema.required).toEqual(['deviceId', 'host', 'deviceType', 'command']);
    expect(Object.keys(tools[1].inputSchema.properties)).toEqual(['deviceId', 'host', 'deviceType', 'command', 'params']);
    expect(tools[2].name).toBe('list-device-types');
    expect(tools[2].inputSchema.required ?? []).toEqual([]);
    expect(tools[3].name).toBe('routeros-write');
    expect(tools[4].name).toBe('temp-radio-plan');
    expect(tools[5].name).toBe('temp-radio-apply');
    expect(tools[6].name).toBe('temp-radio-rollback');
    expect(tools[7].name).toBe('temp-ospf-apply');
    expect(tools[8].name).toBe('temp-ospf-rollback');
    expect(tools[9].name).toBe('temp-mt-flip');
    expect(tools[10].name).toBe('temp-mt-rollback');
    expect(tools[11].name).toBe('temp-ap-wds');
    expect(tools[12].name).toBe('temp-mt-bounce');
    expect(tools[13].name).toBe('temp-mt-alias');
    expect(tools[14].name).toBe('temp-mt-alias-remove');
    expect(tools[15].name).toBe('temp-ospf-plan');
    expect(tools[16].name).toBe('lookup-device-by-exact-ip');
    expect(tools[16].inputSchema.required).toEqual(['ip']);
  });

  it('hides exact-IP lookup when disabled by configuration', async () => {
    const { NocMcpServer } = await import('../src/server.js');
    const server = new NocMcpServer({
      inventory: { enabled: false },
      credentialStore: { path: '', passphraseEnvVar: 'TEST' },
      auditLog: { path: '/tmp/test-audit.log', maxFileSizeMb: 1, maxFiles: 1 },
      limits: { maxConcurrentDevices: 1, commandTimeoutMs: 5000, maxOutputBytes: 1024 },
      redaction: { enabled: true, preserveInternalIps: [], preserveHostnames: [] },
    });

    expect(server.getToolDefinitions().map(tool => tool.name)).not.toContain('lookup-device-by-exact-ip');
    const result = await server.handleToolCall('lookup-device-by-exact-ip', { ip: '10.0.0.1' });
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toContain('disabled by configuration');
  });

  it('rejects exact-device diagnostic without a unique bound credential before network access', async () => {
    vi.resetModules();
    mkdirSync(TEST_DIR, { recursive: true });
    vi.doMock('../src/credentials/store.js', () => ({
      CredentialStore: vi.fn().mockImplementation(() => ({
        load: vi.fn().mockResolvedValue({
          version: 1,
          deviceTypes: [{
            deviceType: 'ubnt-airos',
            credentials: [{ name: 'shared', username: 'ubnt', password: 'shared-password', priority: 0, tags: [] }],
          }],
        }),
      })),
    }));
    const createDeviceClient = vi.fn();
    vi.doMock('../src/devices/factory.js', () => ({ createDeviceClient }));
    process.env.TEST_PASS = 'test-passphrase';
    const { NocMcpServer } = await import('../src/server.js');
    const server = new NocMcpServer({
      credentialStore: { path: join(TEST_DIR, 'creds.enc'), passphraseEnvVar: 'TEST_PASS' },
      auditLog: { path: join(TEST_DIR, 'audit-exact.log'), maxFileSizeMb: 1, maxFiles: 1 },
      limits: { maxConcurrentDevices: 1, commandTimeoutMs: 5000, maxOutputBytes: 1024 },
      redaction: { enabled: true, preserveInternalIps: [], preserveHostnames: [] },
    });
    const result = await server.handleToolCall('run-exact-device-diagnostics', {
      deviceId: 'unknown-cpe', host: '192.0.2.9', deviceType: 'ubnt-airos', command: 'status', params: {},
    });
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toContain('no fallback credentials were attempted');
    expect(createDeviceClient).not.toHaveBeenCalled();
    delete process.env.TEST_PASS;
  });

  it('validates input before execution', async () => {
    const { NocMcpServer } = await import('../src/server.js');
    const server = new NocMcpServer({
      credentialStore: { path: '', passphraseEnvVar: 'TEST' },
      auditLog: { path: '/tmp/test-audit.log', maxFileSizeMb: 1, maxFiles: 1 },
      limits: { maxConcurrentDevices: 1, commandTimeoutMs: 5000, maxOutputBytes: 1024 },
      redaction: { enabled: true, preserveInternalIps: [], preserveHostnames: [] },
    });

    const result = await server.handleToolCall('run-diagnostics', {
      host: '',
      deviceType: 'mikrotik-ssh',
      command: 'system-resource',
    });

    expect(result.isError).toBe(true);
    expect(result.content[0].text).toContain('Validation error');
  });
});

describe('Output size limit enforcement', () => {
  beforeEach(() => {
    vi.resetModules();
    mkdirSync(TEST_DIR, { recursive: true });
  });

  afterEach(() => {
    rmSync(TEST_DIR, { recursive: true, force: true });
    vi.restoreAllMocks();
  });

  it('truncates output when it exceeds maxOutputBytes', async () => {
    const largeData = { entries: 'x'.repeat(1500) };

    vi.doMock('../src/credentials/store.js', () => ({
      CredentialStore: vi.fn().mockImplementation(() => ({
        load: vi.fn().mockResolvedValue({
          version: 1,
          deviceTypes: [{
            deviceType: 'mikrotik-ssh',
            credentials: [{
              name: 'default',
              username: 'admin',
              password: 'pass',
              priority: 0,
              tags: [],
            }],
          }],
        }),
      })),
    }));

    vi.doMock('../src/devices/factory.js', () => ({
      createDeviceClient: vi.fn().mockReturnValue({
        connect: vi.fn().mockResolvedValue(undefined),
        execute: vi.fn().mockResolvedValue({
          device: '10.0.0.1',
          command: 'system-resource',
          category: 'system',
          status: 'success',
          result: largeData,
          warnings: [],
          redactions: 0,
          timestamp: new Date().toISOString(),
          durationMs: 100,
        }),
        disconnect: vi.fn().mockResolvedValue(undefined),
      }),
    }));

    process.env.TEST_PASS = 'test-passphrase';
    const { NocMcpServer } = await import('../src/server.js');

    const server = new NocMcpServer({
      credentialStore: { path: join(TEST_DIR, 'creds.enc'), passphraseEnvVar: 'TEST_PASS' },
      auditLog: { path: join(TEST_DIR, 'audit.log'), maxFileSizeMb: 1, maxFiles: 1 },
      limits: { maxConcurrentDevices: 1, commandTimeoutMs: 5000, maxOutputBytes: 1024 },
      redaction: { enabled: false, preserveInternalIps: [], preserveHostnames: [] },
    });

    const result = await server.handleToolCall('run-diagnostics', {
      host: '10.0.0.1',
      deviceType: 'mikrotik-ssh',
      command: 'system-resource',
    });

    expect(result.isError).toBeFalsy();
    const text = result.content[0].text;
    expect(text.length).toBeLessThanOrEqual(1123);
    expect(text).toContain('[truncated - output exceeded 1024 bytes]');

    delete process.env.TEST_PASS;
  });

  it('does not truncate output when within maxOutputBytes', async () => {
    const smallData = { s: 1 };

    vi.doMock('../src/credentials/store.js', () => ({
      CredentialStore: vi.fn().mockImplementation(() => ({
        load: vi.fn().mockResolvedValue({
          version: 1,
          deviceTypes: [{
            deviceType: 'mikrotik-ssh',
            credentials: [{
              name: 'default',
              username: 'admin',
              password: 'pass',
              priority: 0,
              tags: [],
            }],
          }],
        }),
      })),
    }));

    vi.doMock('../src/devices/factory.js', () => ({
      createDeviceClient: vi.fn().mockReturnValue({
        connect: vi.fn().mockResolvedValue(undefined),
        execute: vi.fn().mockResolvedValue({
          device: '10.0.0.1',
          command: 'system-resource',
          category: 'system',
          status: 'success',
          result: smallData,
          warnings: [],
          redactions: 0,
          timestamp: '2026-01-01T00:00:00.000Z',
          durationMs: 1,
        }),
        disconnect: vi.fn().mockResolvedValue(undefined),
      }),
    }));

    process.env.TEST_PASS = 'test-passphrase';
    const { NocMcpServer } = await import('../src/server.js');

    const server = new NocMcpServer({
      credentialStore: { path: join(TEST_DIR, 'creds.enc'), passphraseEnvVar: 'TEST_PASS' },
      auditLog: { path: join(TEST_DIR, 'audit.log'), maxFileSizeMb: 1, maxFiles: 1 },
      limits: { maxConcurrentDevices: 1, commandTimeoutMs: 5000, maxOutputBytes: 1024 },
      redaction: { enabled: false, preserveInternalIps: [], preserveHostnames: [] },
    });

    const result = await server.handleToolCall('run-diagnostics', {
      host: '10.0.0.1',
      deviceType: 'mikrotik-ssh',
      command: 'system-resource',
    });

    expect(result.isError).toBeFalsy();
    const text = result.content[0].text;
    expect(text).not.toContain('[truncated');
    expect(text).toContain('"s": 1');

    delete process.env.TEST_PASS;
  });
});
