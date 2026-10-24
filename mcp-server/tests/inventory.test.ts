import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { readFileSync, rmSync } from 'fs';

const TEST_DIR = '/tmp/noc-mcp-test-inventory';
const fetchMock = vi.fn();
vi.stubGlobal('fetch', fetchMock);

function response(payload: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(payload) };
}

function makeServer(overrides: Record<string, unknown> = {}) {
  return import('../src/server.js').then(({ NocMcpServer }) => new NocMcpServer({
    credentialStore: { path: '', passphraseEnvVar: 'UNUSED' },
    auditLog: { path: `${TEST_DIR}/audit.log`, maxFileSizeMb: 1, maxFiles: 1 },
    limits: { maxConcurrentDevices: 1, commandTimeoutMs: 5000, maxOutputBytes: 8192 },
    redaction: { enabled: true, preserveInternalIps: [], preserveHostnames: [] },
    ...overrides,
  }));
}

describe('lookup-device-by-exact-ip MCP tool', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doUnmock('../src/credentials/store.js');
    vi.doUnmock('../src/devices/factory.js');
    vi.stubEnv('TEST_LIBRENMS_TOKEN', 'librenms-secret');
    vi.stubEnv('NOC_MCP_PASSPHRASE', 'unused-test-passphrase');
    fetchMock.mockReset();
  });

  afterEach(() => {
    rmSync(TEST_DIR, { recursive: true, force: true });
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('uses configured exact-IP inventory and emits source/freshness without bulk fallback', async () => {
    fetchMock.mockResolvedValue(response({ status: 'ok', devices: [{
      device_id: '3039',
      hostname: '10.0.63.13',
      sysName: 'customer5s',
      hardware: 'LiteAP GPS',
      os: 'airos',
      status: true,
      last_polled: new Date().toISOString(),
    }] }));

    const server = await makeServer({ inventory: { enabled: true, libreNmsTokenEnvVar: 'TEST_LIBRENMS_TOKEN', libreNmsBaseUrl: 'http://inventory.example', staleAfterSeconds: 900, lookupTimeoutMs: 5000 } });
    const result = await server.handleToolCall('lookup-device-by-exact-ip', { ip: '10.0.63.13' });

    expect(result.isError).toBeFalsy();
    const output = JSON.parse(result.content[0].text);
    expect(output).toMatchObject({ source: 'LibreNMS', status: 'matched', queriedIp: '10.0.63.13' });
    expect(output.device).toMatchObject({ sysName: 'customer5s', pollFreshness: 'fresh' });
    expect(output.caution).toContain('does not establish site truth');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0].toString()).toBe('http://inventory.example/api/v0/devices/10.0.63.13');
    expect(readFileSync(`${TEST_DIR}/audit.log`, 'utf8')).not.toContain('librenms-secret');
  });

  it('blocks bad tool inputs before network access', async () => {
    const server = await makeServer();
    const result = await server.handleToolCall('lookup-device-by-exact-ip', { ip: '10.0.0.1/24' });
    expect(result.isError).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('fails closed on HTTP errors without returning response details or retrying bulk inventory', async () => {
    fetchMock.mockResolvedValue(response({ status: 'error', secret: 'should-not-be-returned' }, 403));
    const server = await makeServer({ inventory: { enabled: true, libreNmsTokenEnvVar: 'TEST_LIBRENMS_TOKEN', libreNmsBaseUrl: 'http://inventory.example', staleAfterSeconds: 900, lookupTimeoutMs: 5000 } });
    const result = await server.handleToolCall('lookup-device-by-exact-ip', { ip: '10.0.0.1' });
    expect(result.isError).toBe(true);
    expect(result.content[0].text).toContain('no bulk inventory fallback');
    expect(result.content[0].text).not.toContain('should-not-be-returned');
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
