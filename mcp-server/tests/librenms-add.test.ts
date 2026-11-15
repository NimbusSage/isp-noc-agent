import { describe, it, expect, vi, afterEach } from 'vitest';
import { postLibrenmsDevice } from '../src/inventory/librenms-add.js';

const request = { baseUrl: 'http://inventory.example', token: 'test-token', timeoutMs: 5000 };

function mockFetch(status: number, body: unknown) {
  const spy = vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));
  vi.stubGlobal('fetch', spy);
  return spy;
}

afterEach(() => vi.unstubAllGlobals());

describe('librenms add device', () => {
  it('never sends force_add=true and asks for v2c with the given community', async () => {
    const spy = mockFetch(200, { status: 'ok', message: 'Device added', devices: [{ device_id: 4321 }] });
    const result = await postLibrenmsDevice('10.0.1.9', 'pilotCommunity1', request);
    expect(result).toEqual({ status: 'added', message: 'Device added', deviceId: '4321' });

    const call = spy.mock.calls[0] as unknown as [URL, RequestInit];
    expect(call[0].toString()).toBe('http://inventory.example/api/v0/devices');
    const body = JSON.parse(String(call[1].body)) as Record<string, unknown>;
    expect(body.force_add).toBe(false);
    expect(body.version).toBe('v2c');
    expect(body.community).toBe('pilotCommunity1');
    expect(body.hostname).toBe('10.0.1.9');
  });

  it('surfaces LibreNMS refusing a duplicate sysName instead of forcing it', async () => {
    mockFetch(200, { status: 'error', message: 'Could not add device: already exists' });
    const result = await postLibrenmsDevice('10.0.1.9', 'c', request);
    expect(result.status).toBe('error');
    expect(result.deviceId).toBeNull();
    expect(result.message).toMatch(/already exists/);
  });

  it('treats an HTTP error as a failure even with a status field', async () => {
    mockFetch(500, { status: 'ok', message: 'boom' });
    expect((await postLibrenmsDevice('10.0.1.9', 'c', request)).status).toBe('error');
  });

  it('refuses to run without a token or against a placeholder base URL', async () => {
    await expect(postLibrenmsDevice('10.0.1.9', 'c', { ...request, token: '  ' })).rejects.toThrow(/token/);
    await expect(postLibrenmsDevice('10.0.1.9', 'c', { ...request, baseUrl: 'http://librenms.invalid' })).rejects.toThrow(/not configured/);
  });
});
