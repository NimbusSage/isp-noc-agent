// env_load.mjs - let a Node helper read ONLY the .env settings it needs, so nobody has to `source` the whole file.
// loadEnvPrefixes(['TRACKER_']) copies TRACKER_* values from .env into the process environment when they are not
// already set. Nothing else in the file is read into the environment, and no value is printed or returned.
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** Parse .env text into [name, value] pairs (same syntax rules as scripts/envfile.py, no expansion). */
export function parseDotenv(text) {
  const out = [];
  for (const raw of String(text).replace(/^﻿/, '').split('\n')) {
    const line = raw.replace(/\r$/, '').trim();
    if (!line || line.startsWith('#')) continue;
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!m) continue;
    let v = m[2];
    if (v.startsWith('"')) { const e = v.indexOf('"', 1); v = e > 0 ? v.slice(1, e) : v.slice(1); }
    else if (v.startsWith("'")) { const e = v.indexOf("'", 1); v = e > 0 ? v.slice(1, e) : v.slice(1); }
    else v = v.replace(/\s#.*$/, '').trim();
    out.push([m[1], v]);
  }
  return out;
}

/** Set missing environment variables whose names start with one of `prefixes`. Returns the names it set. */
export function loadEnvPrefixes(prefixes, { env = process.env, file } = {}) {
  const path = file || (env.NOC_DOTENV || '').trim() || resolve(ROOT, '.env');
  let text;
  try { text = readFileSync(path, 'utf8'); } catch { return []; }
  const set = [];
  for (const [name, value] of parseDotenv(text)) {
    if (!prefixes.some((p) => name.startsWith(p))) continue;
    if (typeof env[name] === 'string' && env[name].trim()) continue;
    if (!value.trim()) continue;
    env[name] = value;
    set.push(name);
  }
  return set;
}
