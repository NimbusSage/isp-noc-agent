import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { lookupLibreNmsDeviceByExactIp } from '../src/inventory/librenms.js';

const fetchMock = vi.fn();
vi.stubGlobal('fetch', fetchMock);

const options = {
  baseUrl: 'http://librenms.example',
  token: 'test-token',
  timeoutMs: 5_000,
  now: () => Date.parse('2026-10-01T12:00:00.000Z'),
};

function response(devices: unknown[]) {
  return {
    ok: true,
    text: async () => JSON.stringify({ status: 'ok', devices }),
  };
}

describe('LibreNMS exact-IP inventory lookup', () => {
  beforeEach(() => fetchMock.mockReset());
  afterEach(() => vi.restoreAllMocks());

  it('returns a uniquely matched device and keeps identity distinct from reachability/site truth', async () => {
    fetchMock.mockResolvedValue(response([{
      device_id: 123,
      hostname: '10.0.63.13',
      sysName: 'customer5s',
      hardware: 'LiteAP GPS',
      os: 'airos',
      version: '8.7.19',
      status: true,
      last_polled: new Date(options.now() - 60_000).toISOString(),
      location: 'Customer A',
    }]));

    const result = await lookupLibreNmsDeviceByExactIp('10.0.63.13', options);
    expect(result.status).toBe('matched');
    if (result.status !== 'matched') throw new Error('expected matched result');
    expect(result.device).toMatchObject({
      deviceId: '123',
      sysName: 'customer5s',
      hardware: 'LiteAP GPS',
      libreNmsReportedStatus: true,
      pollFreshness: 'fresh',
      pollAgeSeconds: 60,
    });
    expect(result.caution).toContain('does not establish site truth');
    expect(fetchMock).toHaveBeenCalledWith(
      new URL('/api/v0/devices/10.0.63.13', options.baseUrl),
      expect.objectContaining({
        redirect: 'error',
        headers: expect.objectContaining({ 'X-Auth-Token': options.token }),
      }),
    );
  });

  it('does not assert identity if the exact-IP endpoint returns a different hostname', async () => {
    fetchMock.mockResolvedValue(response([{ hostname: 'other-device', sysName: 'not-the-target' }]));
    const result = await lookupLibreNmsDeviceByExactIp('10.0.0.1', options);
    expect(result.status).toBe('identity_unverified');
    expect(result.caution).toContain('no identity match is asserted');
  });

  it('distinguishes empty and ambiguous inventory responses', async () => {
    fetchMock.mockResolvedValueOnce(response([]));
    expect((await lookupLibreNmsDeviceByExactIp('10.0.0.1', options)).status).toBe('not_found');

    fetchMock.mockResolvedValueOnce(response([{}, {}]));
    expect((await lookupLibreNmsDeviceByExactIp('10.0.0.1', options)).status).toBe('ambiguous');
  });

  it('reports naive local poll timestamps as timezone-unverified', async () => {
    fetchMock.mockResolvedValueOnce(response([{ hostname: '10.0.0.1', last_polled: '2026-10-01 11:00:00' }]));
    const result = await lookupLibreNmsDeviceByExactIp('10.0.0.1', options);
    expect(result.status).toBe('matched');
    if (result.status === 'matched') expect(result.device.pollFreshness).toBe('timezone-unverified');
  });

  it('marks stale, future-dated, and absent poll timestamps explicitly', async () => {
    fetchMock.mockResolvedValueOnce(response([{ hostname: '10.0.0.1', last_polled: new Date(options.now() - 60 * 60_000).toISOString() }]));
    const stale = await lookupLibreNmsDeviceByExactIp('10.0.0.1', options);
    expect(stale.status).toBe('matched');
    if (stale.status === 'matched') expect(stale.device.pollFreshness).toBe('stale');

    fetchMock.mockResolvedValueOnce(response([{ hostname: '10.0.0.1', last_polled: new Date(options.now() + 60 * 60_000).toISOString() }]));
    const future = await lookupLibreNmsDeviceByExactIp('10.0.0.1', options);
    expect(future.status).toBe('matched');
    if (future.status === 'matched') expect(future.device.pollFreshness).toBe('future-dated');

    fetchMock.mockResolvedValueOnce(response([{ hostname: '10.0.0.1' }]));
    const unknown = await lookupLibreNmsDeviceByExactIp('10.0.0.1', options);
    expect(unknown.status).toBe('matched');
    if (unknown.status === 'matched') expect(unknown.device.pollFreshness).toBe('unknown');
  });

  it('rejects invalid IPs, unsafe base URLs, oversized responses, and bad payload shapes', async () => {
    await expect(lookupLibreNmsDeviceByExactIp('host.example', options)).rejects.toThrow('IPv4');
    await expect(lookupLibreNmsDeviceByExactIp('10.0.0.1', { ...options, baseUrl: 'http://user:pass@host' })).rejects.toThrow('trusted HTTP(S) origin');

    fetchMock.mockResolvedValueOnce({ ok: true, text: async () => 'x'.repeat(512 * 1024 + 1) });
    await expect(lookupLibreNmsDeviceByExactIp('10.0.0.1', options)).rejects.toThrow('size limit');

    fetchMock.mockResolvedValueOnce({ ok: true, text: async () => '{"status":"ok"}' });
    await expect(lookupLibreNmsDeviceByExactIp('10.0.0.1', options)).rejects.toThrow('devices array');
  });
});
