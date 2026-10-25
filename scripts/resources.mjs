// resources.mjs - Node side of the resource registry (see scripts/resources.py for the full description).
// Same lookup order: environment variable (process environment, then .env), then the profile
// "resources" block (site-pack/profile.json), then an older location named in resource_catalog.json.
// A resource that is not registered is not available: callers stop and ask the operator.
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseDotenvValue } from './cred_store.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
export const ROOT = resolve(HERE, '..');
const SHEET_ID = /^[A-Za-z0-9_-]{20,100}$/;

export const notRegistered = (name) =>
  `${name} is not registered. Ask the operator for its address and stop; do not take one from a skill example, `
  + `another system's device list or DNS. The operator registers it with: python3 scripts/resources.py set ${name} <value>`;

/** Canonical form of a resource value (same rules as resources.py normalize); throws on invalid input. */
export function normalizeResource(value, kind) {
  let text = String(value ?? '').trim();
  if (!text) throw new Error('empty value');
  if (kind === 'sheet') {
    const m = /\/spreadsheets\/d\/([A-Za-z0-9_-]+)/.exec(text);
    text = m ? m[1] : text;
    if (!SHEET_ID.test(text)) throw new Error('not a Google spreadsheet id (20-100 letters, digits, - or _) or sheet URL');
    return text;
  }
  if (!text.includes('://')) text = `https://${text}`;
  let u;
  try { u = new URL(text); } catch { throw new Error('not a http(s) URL or host name'); }
  if (!['http:', 'https:'].includes(u.protocol) || !u.hostname) throw new Error('not a http(s) URL or host name');
  if (u.username || u.password || u.search || u.hash) throw new Error('a resource URL must not carry credentials, a query or a fragment');
  return `${u.protocol}//${u.host}${u.pathname.replace(/\/+$/, '')}`;
}

export function loadCatalog() {
  return JSON.parse(readFileSync(resolve(HERE, 'resource_catalog.json'), 'utf8')).resources;
}

/**
 * Resolve one resource: { name, kind, value, source, others, problems }.
 * opts.env: environment (default process.env); opts.readText(absPath): file reader (default fs, injectable for tests).
 */
export function resolveResource(name, opts = {}) {
  const env = opts.env ?? process.env;
  const readText = opts.readText ?? ((f) => readFileSync(f, 'utf8'));
  const profilePath = (env.NOC_PROFILE_FILE || '').trim() || resolve(ROOT, 'site-pack', 'profile.json');
  const pack = (env.NOC_SITE_PACK_DIR || '').trim() || dirname(profilePath);
  const dotenvPath = (env.NOC_DOTENV || '').trim() || resolve(ROOT, '.env');
  const tryJson = (file) => { try { const d = JSON.parse(readText(file)); return d && typeof d === 'object' ? d : {}; } catch { return {}; } };
  const getenv = (n) => {
    const v = env[n];
    if (typeof v === 'string' && v.trim()) return v.trim();
    try { return parseDotenvValue(readText(dotenvPath), n).trim(); } catch { return ''; }
  };
  const profile = tryJson(profilePath);
  const registered = profile.resources && typeof profile.resources === 'object' ? profile.resources : {};
  const cat = loadCatalog();
  let entry = cat[name];
  const custom = registered[name] && typeof registered[name] === 'object' ? registered[name] : null;
  if (!entry) {
    if (!custom) throw new Error(`unknown resource '${name}'; known: ${[...new Set([...Object.keys(cat), ...Object.keys(registered)])].sort().join(', ')}`);
    entry = { kind: custom.kind || 'url', env: [], legacy: [] };
  }
  const kind = entry.kind || 'url';
  const raw = [];
  for (const v of entry.env || []) { const x = getenv(v); if (x) raw.push([`env ${v}`, x]); }
  if (custom) { const x = custom[kind === 'sheet' ? 'id' : 'url']; if (typeof x === 'string' && x.trim()) raw.push(['profile', x]); }
  for (const leg of entry.legacy || []) {
    let x; let where;
    if (leg.profile) { x = leg.profile.reduce((n, k) => (n && typeof n === 'object' ? n[k] : undefined), profile); where = `legacy profile ${leg.profile.join('.')}`; }
    else { x = tryJson(resolve(pack, leg.file))[leg.key]; where = `legacy file ${leg.file}`; }
    if (typeof x === 'string' && x.trim()) raw.push([where, x]);
  }
  const res = { name, kind, value: '', source: '', others: [], problems: [] };
  const seen = [];
  for (const [src, v] of raw) {
    try { seen.push([src, normalizeResource(v, kind)]); } catch (e) { res.problems.push(`${src}: ${e.message}`); }
  }
  if (seen.length) {
    [res.source, res.value] = seen[0];
    res.others = seen.slice(1).filter(([, v]) => v !== res.value);
  }
  return res;
}

/** The registered value, or an Error whose message tells the caller what to do next. */
export function getResource(name, opts = {}) {
  const r = resolveResource(name, opts);
  if (!r.value) throw new Error(r.problems.length ? `${name}: ${r.problems[0]}` : notRegistered(name));
  return r.value;
}
