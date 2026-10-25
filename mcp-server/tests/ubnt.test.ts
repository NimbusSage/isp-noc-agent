import { describe, it, expect, vi, beforeEach } from 'vitest';
import { UBNTUispClient } from '../src/devices/ubnt-uisp.js';
import { UBNTAirosClient } from '../src/devices/ubnt-airos.js';
import { UBNT_AEROS_COMMANDS, UBNT_UISP_COMMANDS } from '../src/commands/allowlist.js';
import type { DeviceTypeCredential } from '../src/credentials/schema.js';

// Mock fetch
const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

describe('UBNTUispClient', () => {
  let client: UBNTUispClient;
  const mockCredential: DeviceTypeCredential = {
    name: 'api',
    username: 'admin',
    password: 'pass',
    apiToken: 'test-token-123',
    priority: 0,
    tags: [],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    client = new UBNTUispClient('10.0.0.2', 443, mockCredential);
  });

  it('builds correct API URL', () => {
    const url = client.buildUrl('/nms/api/v2.1/devices');
    expect(url).toBe('https://10.0.0.2:443/nms/api/v2.1/devices');
  });

  it('connect() requests an absolute URL, not a relative path', async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ version: '1' }) });
    await client.connect();
    expect(mockFetch).toHaveBeenCalledWith(
      'https://10.0.0.2:443/nms/api/v2.1/nms/info',
      expect.any(Object)
    );
  });

  it('includes auth header', () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ data: [] }),
    });

    client.execute(UBNT_UISP_COMMANDS['device-list'], {});
    expect(mockFetch).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        headers: expect.objectContaining({
          'x-auth-token': 'test-token-123',
        }),
      })
    );
  });

  it('returns structured result', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ data: [{ id: 'abc', name: 'AP-01' }] }),
    });

    const result = await client.execute(UBNT_UISP_COMMANDS['device-list'], {});
    expect(result.status).toBe('success');
    expect(result.command).toBe('device-list');
  });

  it('enforces command timeout', async () => {
    const slowClient = new UBNTUispClient('10.0.0.2', 443, mockCredential, 100);
    mockFetch.mockImplementation((_url: string, opts?: { signal?: AbortSignal }) => {
      return new Promise((_, reject) => {
        if (opts?.signal) {
          opts.signal.addEventListener('abort', () => {
            reject(new DOMException('The operation timed out.', 'TimeoutError'));
          });
        }
      });
    });

    const result = await slowClient.execute(UBNT_UISP_COMMANDS['device-list'], {});
    expect(result.status).toBe('error');
    expect(result.warnings[0]).toContain('timed out');
  }, 10000);
});

describe('UBNTAirosClient', () => {
  let client: UBNTAirosClient;
  const mockCredential: DeviceTypeCredential = {
    name: 'local',
    username: 'ubnt',
    password: 'ubnt',
    priority: 0,
    tags: [],
  };

  beforeEach(() => {
    vi.clearAllMocks();
    client = new UBNTAirosClient('10.0.0.3', 80, mockCredential);
  });

  it('builds correct airOS URL', () => {
    const url = client.buildUrl('/status.cgi');
    expect(url).toBe('http://10.0.0.3:80/status.cgi');
  });

  // Transport note: UBNTAirosClient uses node:http(s) directly (plant radios
  // redirect http→https with self-signed certs, which global fetch rejects),
  // so these tests run against a real local HTTP server instead of fetch mocks.
  async function withServer(handler: (req: import('node:http').IncomingMessage, res: import('node:http').ServerResponse) => void) {
    const { createServer } = await import('node:http');
    const seen: { auth?: string; url?: string } = {};
    const server = createServer((req, res) => {
      seen.auth = req.headers.authorization;
      seen.url = req.url;
      handler(req, res);
    });
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
    const port = (server.address() as import('node:net').AddressInfo).port;
    return { server, seen, port };
  }

  it('uses basic auth', async () => {
    const { server, seen, port } = await withServer((_req, res) => {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ hostname: 'AP-02' }));
    });
    try {
      const c = new UBNTAirosClient('127.0.0.1', port, mockCredential);
      const result = await c.execute(UBNT_AEROS_COMMANDS['status'], {});
      expect(result.status).toBe('success');
      expect((result.result as { hostname: string }).hostname).toBe('AP-02');
      expect(seen.auth ?? '').toMatch(/^Basic /);
    } finally {
      server.close();
    }
  });

  it('follows the plant http→https-style redirect', async () => {
    const { server, port } = await withServer((req, res) => {
      if (req.url === '/status.cgi') {
        res.writeHead(302, { Location: '/real' });
        res.end();
        return;
      }
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ hostname: 'AP-03' }));
    });
    try {
      const c = new UBNTAirosClient('127.0.0.1', port, mockCredential);
      const r = await c.execute(UBNT_AEROS_COMMANDS['status'], {});
      expect(r.status).toBe('success');
      expect((r.result as { hostname: string }).hostname).toBe('AP-03');
    } finally {
      server.close();
    }
  });

  it('enforces command timeout', async () => {
    const { server, port } = await withServer((_req, _res) => {
      // Never respond — client timeout must fire.
    });
    try {
      const slowClient = new UBNTAirosClient('127.0.0.1', port, mockCredential, 100);
      const result = await slowClient.execute(UBNT_AEROS_COMMANDS['status'], {});
      expect(result.status).toBe('error');
      expect(result.warnings[0]).toContain('timed out');
    } finally {
      server.close();
    }
  }, 10000);
});
