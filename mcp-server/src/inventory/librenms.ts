import { isIP } from 'node:net';

const MAX_RESPONSE_BYTES = 512 * 1024;
const DEFAULT_STALE_AFTER_SECONDS = 15 * 60;
const FUTURE_CLOCK_SKEW_SECONDS = 5 * 60;

export interface LibreNmsDeviceSummary {
  deviceId: string | null;
  hostname: string | null;
  sysName: string | null;
  hardware: string | null;
  os: string | null;
  version: string | null;
  libreNmsReportedStatus: boolean | null;
  location: string | null;
  lastPolled: string | null;
  pollFreshness: 'fresh' | 'stale' | 'future-dated' | 'timezone-unverified' | 'unknown';
  pollAgeSeconds: number | null;
}

export interface ExactIpLookupResult {
  status: 'matched' | 'not_found' | 'ambiguous' | 'identity_unverified';
  queriedIp: string;
  source: 'LibreNMS';
  retrievedAt: string;
  matchBasis: 'exact-IP endpoint; hostname must equal queried IP to assert identity';
  device?: LibreNmsDeviceSummary;
  caution: string;
}

function asString(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return value.trim();
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
}

function parseDeviceStatus(value: unknown): boolean | null {
  if (typeof value === 'boolean') return value;
  if (value === 1 || value === '1' || value === 'true') return true;
  if (value === 0 || value === '0' || value === 'false') return false;
  return null;
}

function parsePollTime(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 1_000_000_000_000 ? value : value * 1000;
  }
  if (typeof value !== 'string' || !value.trim()) return null;

  const trimmed = value.trim();
  if (/^\d+(?:\.\d+)?$/.test(trimmed)) {
    const numeric = Number(trimmed);
    return numeric > 1_000_000_000_000 ? numeric : numeric * 1000;
  }
  // Do not guess the timezone for a server-local-looking timestamp. The caller
  // reports freshness as timezone-unverified unless the API supplies an offset.
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(trimmed)) return null;
  const parsed = Date.parse(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function summarizeDevice(device: Record<string, unknown>, nowMs: number, staleAfterSeconds: number): LibreNmsDeviceSummary {
  const lastPolled = asString(device.last_polled);
  const polledMs = parsePollTime(device.last_polled);
  let pollFreshness: LibreNmsDeviceSummary['pollFreshness'] = 'unknown';
  let pollAgeSeconds: number | null = null;

  if (lastPolled && polledMs === null && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/.test(lastPolled)) {
    pollFreshness = 'timezone-unverified';
  }

  if (polledMs !== null) {
    pollAgeSeconds = Math.floor((nowMs - polledMs) / 1000);
    if (pollAgeSeconds < -FUTURE_CLOCK_SKEW_SECONDS) {
      pollFreshness = 'future-dated';
    } else if (pollAgeSeconds < 0) {
      pollFreshness = 'fresh';
    } else {
      pollFreshness = pollAgeSeconds <= staleAfterSeconds ? 'fresh' : 'stale';
    }
  }

  return {
    deviceId: asString(device.device_id),
    hostname: asString(device.hostname),
    sysName: asString(device.sysName),
    hardware: asString(device.hardware),
    os: asString(device.os),
    version: asString(device.version),
    libreNmsReportedStatus: parseDeviceStatus(device.status),
    location: typeof device.location === 'string' ? device.location : null,
    lastPolled,
    pollFreshness,
    pollAgeSeconds,
  };
}

function assertSafeBaseUrl(baseUrl: string): URL {
  let parsed: URL;
  try {
    parsed = new URL(baseUrl);
  } catch {
    throw new Error('LibreNMS base URL is invalid');
  }
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash || (parsed.pathname !== '/' && parsed.pathname !== '')) {
    throw new Error('LibreNMS base URL must be a trusted HTTP(S) origin without credentials, path, query, or fragment');
  }
  return parsed;
}

async function readResponseBodyLimited(response: Response): Promise<string> {
  if (!response.body) {
    const text = await response.text();
    if (Buffer.byteLength(text, 'utf8') > MAX_RESPONSE_BYTES) {
      throw new Error('LibreNMS exact-IP response exceeded the size limit');
    }
    return text;
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      totalBytes += value.byteLength;
      if (totalBytes > MAX_RESPONSE_BYTES) {
        await reader.cancel();
        throw new Error('LibreNMS exact-IP response exceeded the size limit');
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const combined = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(combined);
}

export async function lookupLibreNmsDeviceByExactIp(
  ip: string,
  options: {
    baseUrl: string;
    token: string;
    timeoutMs: number;
    staleAfterSeconds?: number;
    now?: () => number;
  },
): Promise<ExactIpLookupResult> {
  if (isIP(ip) !== 4) throw new Error('A valid IPv4 address is required');
  if (!options.token.trim()) throw new Error('LibreNMS API token is not configured');

  const baseUrl = assertSafeBaseUrl(options.baseUrl);
  if (baseUrl.hostname.toLowerCase().endsWith('.invalid')) {
    throw new Error('LibreNMS base URL is not configured (set LIBRENMS_URL or inventory.libreNmsBaseUrl)');
  }
  const now = options.now ?? Date.now;
  const retrievedAt = new Date(now()).toISOString();
  const response = await fetch(new URL(`/api/v0/devices/${encodeURIComponent(ip)}`, baseUrl), {
    headers: { 'X-Auth-Token': options.token, Accept: 'application/json' },
    signal: AbortSignal.timeout(options.timeoutMs),
    redirect: 'error',
  });
  if (!response.ok) throw new Error(`LibreNMS lookup failed with HTTP ${response.status}`);

  const text = await readResponseBodyLimited(response);

  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    throw new Error('LibreNMS exact-IP response was not valid JSON');
  }
  if (!body || typeof body !== 'object' || !Array.isArray((body as { devices?: unknown }).devices)) {
    throw new Error('LibreNMS exact-IP response did not contain a devices array');
  }

  const devices = (body as { devices: unknown[] }).devices;
  const caution = 'LibreNMS is identity/status corroboration only: this result does not establish site truth, radio role, current reachability, RF compatibility, or topology.';
  const baseResult = {
    queriedIp: ip,
    source: 'LibreNMS' as const,
    retrievedAt,
    matchBasis: 'exact-IP endpoint; hostname must equal queried IP to assert identity' as const,
    caution,
  };

  if (devices.length === 0) return { ...baseResult, status: 'not_found' };
  if (devices.length !== 1 || !devices[0] || typeof devices[0] !== 'object' || Array.isArray(devices[0])) {
    return { ...baseResult, status: 'ambiguous' };
  }

  const device = summarizeDevice(devices[0] as Record<string, unknown>, now(), options.staleAfterSeconds ?? DEFAULT_STALE_AFTER_SECONDS);
  if (device.hostname !== ip) {
    return {
      ...baseResult,
      status: 'identity_unverified',
      caution: `${caution} The API returned one record, but its hostname was not the exact queried IPv4; no identity match is asserted.`,
      device,
    };
  }

  return { ...baseResult, status: 'matched', device };
}
