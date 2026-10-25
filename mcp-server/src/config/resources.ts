/**
 * Where this organization's own services live (the TypeScript side of scripts/resources.py).
 *
 * Same lookup order as the Python and Node resolvers: environment variable, then the
 * "resources" block of site-pack/profile.json, then an older profile key named in
 * scripts/resource_catalog.json. Nothing here makes a network call or reads a secret.
 *
 * Why the MCP server needs it: a registered service host is the only non-private address a
 * device tool may connect to without being listed in NOC_READ_ALLOWED_HOSTS, and only with the
 * device type that service uses (see security/hosts.ts).
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url)); // <repo>/mcp-server/{src,dist}/config
const REPO_ROOT = resolve(HERE, '..', '..', '..');

interface LegacyLocation { profile?: string[]; file?: string; key?: string }
interface CatalogEntry { kind?: string; env?: string[]; legacy?: LegacyLocation[]; mcp_device_type?: string }
type Json = Record<string, unknown>;

function readJson(file: string): Json {
  try {
    const data: unknown = JSON.parse(readFileSync(file, 'utf8'));
    return data && typeof data === 'object' && !Array.isArray(data) ? (data as Json) : {};
  } catch {
    return {};
  }
}

function asRecord(value: unknown): Json {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Json) : {};
}

function catalog(repoRoot: string): Record<string, CatalogEntry> {
  return asRecord(readJson(resolve(repoRoot, 'scripts', 'resource_catalog.json')).resources) as Record<string, CatalogEntry>;
}

/** Canonical http(s) URL (https:// assumed for a bare host, trailing slash dropped), or '' when unusable. */
export function normalizeServiceUrl(value: string): string {
  const text = value.trim();
  if (!text) return '';
  try {
    const u = new URL(text.includes('://') ? text : `https://${text}`);
    if (!['http:', 'https:'].includes(u.protocol) || !u.hostname || u.username || u.password || u.search || u.hash) return '';
    return `${u.protocol}//${u.host}${u.pathname.replace(/\/+$/, '')}`;
  } catch {
    return '';
  }
}

/** Candidate URLs for one resource, best first (environment, profile, older profile key). */
export function registeredUrls(resource: string, env: NodeJS.ProcessEnv = process.env, repoRoot: string = REPO_ROOT): string[] {
  const entry = catalog(repoRoot)[resource];
  const profilePath = env.NOC_PROFILE_FILE?.trim() || resolve(repoRoot, 'site-pack', 'profile.json');
  const profile = readJson(profilePath);
  const out: string[] = [];
  const push = (v: unknown): void => {
    if (typeof v !== 'string') return;
    const url = normalizeServiceUrl(v);
    if (url && !out.includes(url)) out.push(url);
  };
  for (const name of entry?.env ?? []) push(env[name]);
  push(asRecord(asRecord(profile.resources)[resource]).url);
  for (const leg of entry?.legacy ?? []) {
    if (!leg.profile) continue;
    let node: unknown = profile;
    for (const k of leg.profile) node = asRecord(node)[k];
    push(node);
  }
  return out;
}

/** The registered URL for a resource, or '' when it is not registered. */
export function registeredUrl(resource: string, env: NodeJS.ProcessEnv = process.env, repoRoot: string = REPO_ROOT): string {
  return registeredUrls(resource, env, repoRoot)[0] ?? '';
}

/** Host names of the registered services that use this MCP device type (for example ubnt-uisp -> the UISP host). */
export function registeredHostsForDeviceType(deviceType: string, env: NodeJS.ProcessEnv = process.env, repoRoot: string = REPO_ROOT): string[] {
  const hosts: string[] = [];
  for (const [name, entry] of Object.entries(catalog(repoRoot))) {
    if (entry.mcp_device_type !== deviceType) continue;
    for (const url of registeredUrls(name, env, repoRoot)) hosts.push(new URL(url).hostname.replace(/^\[|\]$/g, '').toLowerCase());
  }
  return hosts;
}
