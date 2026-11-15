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
    status: 200,
    ok: true,
    text: async () => JSON.stringify({ status: 'ok', devices }),
  };
}

/** LibreNMS's real 404 for an address it does not monitor. */
function notFound(ip: string) {
  return {
    status: 404,
    ok: false,
    text: async () => JSON.stringify({ status: 'error', message: `Device ${ip} does not exist` }),
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
    expect(result.searchUsed).toBe('device-endpoint');
    expect(result.caution).toContain('does not establish site truth');
    expect(fetchMock).toHaveBeenCalledTimes(1);
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

  it('treats a 404 from the endpoint as "not monitored", not as an integration failure', async () => {
    // This is the bug that made every unmonitored device look like a broken
    // LibreNMS integration: 404 is the API's normal answer for a missing device.
    fetchMock.mockResolvedValueOnce(notFound('10.0.0.1'));
    fetchMock.mockResolvedValueOnce(response([]));
    const result = await lookupLibreNmsDeviceByExactIp('10.0.0.1', options);
    expect(result.status).toBe('not_found');
    expect(result.searchUsed).toBe('ipv4-search');
    expect(result.notFoundDetail).toBe('Device 10.0.0.1 does not exist');
    expect(result.caution).toContain('no device at this address');
    // Still bounded: one endpoint request, one filtered search.
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenLastCalledWith(
      new URL('/api/v0/devices?type=ipv4&query=10.0.0.1', options.baseUrl),
      expect.anything(),
    );
  });

  it('finds a device polled by DNS name through the filtered ipv4 search', async () => {
    // hostname is the DNS name, so /devices/{ip} 404s; the address lives in
    // LibreNMS's ipv4 table and must still count as monitored.
    fetchMock.mockResolvedValueOnce(notFound('10.0.0.1'));
    fetchMock.mockResolvedValueOnce(response([
      { device_id: 7, hostname: 'tower5-sw.example', sysName: 'tower5-sw', ip: '10.0.0.1', status: 1, last_polled: new Date(options.now() - 120_000).toISOString() },
    ]));
    const result = await lookupLibreNmsDeviceByExactIp('10.0.0.1', options);
    expect(result.status).toBe('matched');
    if (result.status !== 'matched') throw new Error('expected matched result');
    expect(result.searchUsed).toBe('ipv4-search');
    expect(result.matchBasis).toContain('ipv4 search');
    expect(result.device).toMatchObject({ deviceId: '7', hostname: 'tower5-sw.example', pollFreshness: 'fresh' });
  });

  it('matches on overwrite_ip', async () => {
    fetchMock.mockResolvedValueOnce(notFound('10.0.0.1'));
    fetchMock.mockResolvedValueOnce(response([{ hostname: 'radio.example', ip: '192.0.2.9', overwrite_ip: '10.0.0.1' }]));
    const result = await lookupLibreNmsDeviceByExactIp('10.0.0.1', options);
    expect(result.status).toBe('matched');
  });

  it('refuses to assert identity when the search returns only a different address', async () => {
    fetchMock.mockResolvedValueOnce(notFound('10.0.0.1'));
    fetchMock.mockResolvedValueOnce(response([{ hostname: 'other', ip: '192.0.2.44' }]));
    const result = await lookupLibreNmsDeviceByExactIp('10.0.0.1', options);
    expect(result.status).toBe('identity_unverified');
    expect(result.caution).toContain('not the exact queried IPv4');
  });

  it('reports ambiguity when more than one device claims the address', async () => {
    fetchMock.mockResolvedValueOnce(notFound('10.0.0.1'));
    fetchMock.mockResolvedValueOnce(response([{ hostname: 'a', ip: '10.0.0.1' }, { hostname: 'b', ip: '10.0.0.1' }]));
    expect((await lookupLibreNmsDeviceByExactIp('10.0.0.1', options)).status).toBe('ambiguous');
  });

  it('still fails loudly on real API errors, including in the fallback', async () => {
    fetchMock.mockResolvedValueOnce({ status: 401, ok: false, text: async () => '{"status":"error","message":"Unauthorized"}' });
    await expect(lookupLibreNmsDeviceByExactIp('10.0.0.1', options)).rejects.toThrow('HTTP 401');

    fetchMock.mockResolvedValueOnce(notFound('10.0.0.1'));
    fetchMock.mockResolvedValueOnce({ status: 500, ok: false, text: async () => 'boom' });
    await expect(lookupLibreNmsDeviceByExactIp('10.0.0.1', options)).rejects.toThrow('ipv4 search failed with HTTP 500');
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

    fetchMock.mockResolvedValueOnce({ status: 200, ok: true, text: async () => 'x'.repeat(512 * 1024 + 1) });
    await expect(lookupLibreNmsDeviceByExactIp('10.0.0.1', options)).rejects.toThrow('size limit');

    fetchMock.mockResolvedValueOnce({ status: 200, ok: true, text: async () => '{"status":"ok"}' });
    await expect(lookupLibreNmsDeviceByExactIp('10.0.0.1', options)).rejects.toThrow('devices array');
  });
});
