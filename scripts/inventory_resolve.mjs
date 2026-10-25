#!/usr/bin/env node
// Resolve a radio/site name to device IDs + management IPs via a cached
// MyMate device index. The index endpoint always dumps the full catalog
// (~35 MB), so it is fetched ONCE and cached; lookups hit the cache.
// Usage: node scripts/inventory_resolve.mjs <name-fragment> [--refresh]
// Prints: id, name, mgmt_ip, status, site. Status is monitoring state,
// not proof of current reachability. Never paste secrets.
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getResource } from './resources.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE = resolve(ROOT, 'logs', 'mymate-device-index.json');
const MAX_AGE_MS = 24 * 3600 * 1000;

function loadProjectEnv(file = resolve(ROOT, '.env')) {
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

function mergeCookies(jar, setCookies) {
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

async function login() {
  const base = getResource('mymate');   // throws with the next action when MyMate is not registered
  if (!process.env.MYMATE_USER || !process.env.MYMATE_PASS) throw new Error('MYMATE_USER / MYMATE_PASS not configured');
  let jar = [];
  const csrf = await fetch(base + '/sanctum/csrf-cookie', { redirect: 'manual' });
  jar = mergeCookies(jar, csrf.headers.getSetCookie?.() || []);
  const xsrf = decodeURIComponent((jar.find((c) => c.startsWith('XSRF-TOKEN=')) || '=').split('=').slice(1).join('=') || '');
  const loginRes = await fetch(base + '/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-XSRF-TOKEN': xsrf, Cookie: jar.join('; '), Origin: base, Referer: base + '/' },
    body: JSON.stringify({ email: process.env.MYMATE_USER, password: process.env.MYMATE_PASS }),
  });
  if (!loginRes.ok) throw new Error(`MyMate login failed: HTTP ${loginRes.status}`);
  jar = mergeCookies(jar, loginRes.headers.getSetCookie?.() || []);
  return { base, cookie: jar.join('; ') };
}

async function refreshCache() {
  const { base, cookie } = await login();
  console.error('Fetching full device index (~35 MB, one-time)...');
  const res = await fetch(base + '/api/devices', {
    headers: { Accept: 'application/json', Origin: base, Referer: base + '/', Cookie: cookie },
    signal: AbortSignal.timeout(300000),
  });
  if (!res.ok) throw new Error(`Device index fetch failed: HTTP ${res.status}`);
  const body = await res.json();
  const items = (body.data || []).map((d) => ({ id: d.id, name: d.name, mgmt_ip: d.mgmt_ip, status: d.status, site: d.site_name || null }));
  mkdirSync(dirname(CACHE), { recursive: true });
  writeFileSync(CACHE, JSON.stringify({ fetchedAt: new Date().toISOString(), count: items.length, devices: items }));
  console.error(`Cached ${items.length} devices.`);
  return items;
}

const wantRefresh = process.argv.includes('--refresh');
const frag = process.argv.slice(2).find((a) => !a.startsWith('--'));
if (!frag) { console.error('Usage: node scripts/inventory_resolve.mjs <name-fragment> [--refresh]'); process.exit(1); }
loadProjectEnv();

let cache = null;
try {
  const st = statSync(CACHE);
  if (Date.now() - st.mtimeMs < MAX_AGE_MS && !wantRefresh) cache = JSON.parse(readFileSync(CACHE, 'utf8'));
} catch { /* refetch */ }
const devices = cache?.devices || await refreshCache();
const needle = frag.toLowerCase();
const hits = devices.filter((d) => (d.name || '').toLowerCase().includes(needle) || (d.site || '').toLowerCase().includes(needle));
console.log(JSON.stringify({ fragment: frag, matches: hits.slice(0, 20), totalMatches: hits.length, cacheAge: cache ? 'cached' : 'fresh' }, null, 2));
