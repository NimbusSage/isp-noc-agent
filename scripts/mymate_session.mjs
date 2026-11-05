// MyMate session helpers shared by the MyMate scripts: project .env loading, the Sanctum SPA login and a
// read-only GET. The base URL comes from the resource registry (scripts/resources.py get mymate) and the
// credentials from MYMATE_USER / MYMATE_PASS; nothing is ever printed or passed on a command line.
import http from 'node:http';
import https from 'node:https';
import { mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getResource } from './resources.mjs';

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export function loadProjectEnv(file = (process.env.NOC_DOTENV || '').trim() || resolve(ROOT, '.env')) {
  let text;
  try { text = readFileSync(file, 'utf8'); } catch { return; }
  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!m || Object.hasOwn(process.env, m[1])) continue;
    let v = m[2];
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) v = v.slice(1, -1);
    process.env[m[1]] = v;
  }
}

export function mergeCookies(jar, setCookies) {
  const map = new Map(jar.map((c) => {
    const i = c.indexOf('=');
    return [c.slice(0, i), c];
  }));
  for (const c of setCookies || []) {
    const pair = c.split(';')[0];
    map.set(pair.slice(0, pair.indexOf('=')), pair);
  }
  return [...map.values()];
}

// One HTTP(S) request to the registered MyMate base. MyMate serves a self-signed certificate (the skill's curl
// recipes use -k), so certificate checks are skipped for THIS request only, never process-wide.
export function mymateRequest(url, { method = 'GET', headers = {}, body = null, timeoutMs = 60000 } = {}) {
  const u = new URL(url);
  const mod = u.protocol === 'https:' ? https : http;
  const payload = body == null ? null : Buffer.from(body);
  return new Promise((resolvePromise, reject) => {
    const req = mod.request({
      method, host: u.hostname, port: u.port, path: u.pathname + u.search,
      ...(u.protocol === 'https:' ? { rejectUnauthorized: false } : {}),
      headers: { ...headers, ...(payload ? { 'Content-Length': payload.length } : {}) },
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolvePromise({ status: res.statusCode, setCookies: res.headers['set-cookie'] || [], text: Buffer.concat(chunks).toString('utf8') }));
      res.on('error', reject);
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`MyMate request timed out after ${Math.round(timeoutMs / 1000)}s`)));
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

// Sanctum SPA login: CSRF cookie, then POST /login with the XSRF header. Returns { base, cookie }.
export async function login() {
  const base = getResource('mymate');   // throws with the next action when MyMate is not registered
  if (!process.env.MYMATE_USER || !process.env.MYMATE_PASS) throw new Error('MYMATE_USER / MYMATE_PASS not configured');
  let jar = [];
  const csrf = await mymateRequest(base + '/sanctum/csrf-cookie', { headers: { Accept: 'application/json' } });
  jar = mergeCookies(jar, csrf.setCookies);
  const xsrf = decodeURIComponent((jar.find((c) => c.startsWith('XSRF-TOKEN=')) || '=').split('=').slice(1).join('=') || '');
  const loginRes = await mymateRequest(base + '/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-XSRF-TOKEN': xsrf, Cookie: jar.join('; '), Origin: base, Referer: base + '/' },
    body: JSON.stringify({ email: process.env.MYMATE_USER, password: process.env.MYMATE_PASS }),
  });
  if (loginRes.status < 200 || loginRes.status >= 300) throw new Error(`MyMate login failed: HTTP ${loginRes.status}`);
  jar = mergeCookies(jar, loginRes.setCookies);
  return { base, cookie: jar.join('; ') };
}

// Read-only GET of a MyMate API path ("/api/..."); returns the parsed JSON body.
export async function apiGet(session, apiPath, timeoutMs = 60000) {
  const res = await mymateRequest(session.base + apiPath, {
    headers: { Accept: 'application/json', Origin: session.base, Referer: session.base + '/', Cookie: session.cookie },
    timeoutMs,
  });
  if (res.status < 200 || res.status >= 300) throw new Error(`MyMate GET ${apiPath.split('?')[0]} failed: HTTP ${res.status}`);
  try { return JSON.parse(res.text); } catch { throw new Error(`MyMate GET ${apiPath.split('?')[0]} returned non-JSON`); }
}

// The device index (/api/devices) is ~35 MB and the only name -> id map, so it is fetched once and cached for a day
// in <log dir>/mymate-device-index.json (id, name, mgmt_ip, status, site only). The log dir is NOC_AGENT_LOG_DIR
// (set by the server for its agents), else <repo>/logs.
export const INDEX_MAX_AGE_MS = 24 * 3600 * 1000;
export function logDir() {
  return (process.env.NOC_AGENT_LOG_DIR || '').trim() || resolve(ROOT, 'logs');
}

export async function loadDeviceIndex({ refresh = false, session = null, cacheFile = resolve(logDir(), 'mymate-device-index.json'), log = (m) => console.error(m) } = {}) {
  if (!refresh) {
    try {
      const st = statSync(cacheFile);
      if (Date.now() - st.mtimeMs < INDEX_MAX_AGE_MS) {
        const cache = JSON.parse(readFileSync(cacheFile, 'utf8'));
        return { devices: cache.devices || [], fetchedAt: cache.fetchedAt || null, fresh: false };
      }
    } catch { /* refetch */ }
  }
  const s = session || await login();
  log('Fetching full device index (~35 MB, one-time)...');
  const body = await apiGet(s, '/api/devices', 300000);
  const devices = (body.data || []).map((d) => ({ id: d.id, name: d.name, mgmt_ip: d.mgmt_ip, status: d.status, site: d.site_name || null }));
  const fetchedAt = new Date().toISOString();
  mkdirSync(dirname(cacheFile), { recursive: true });
  writeFileSync(cacheFile, JSON.stringify({ fetchedAt, count: devices.length, devices }));
  log(`Cached ${devices.length} devices.`);
  return { devices, fetchedAt, fresh: true };
}
