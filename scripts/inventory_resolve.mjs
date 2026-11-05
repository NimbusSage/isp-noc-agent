#!/usr/bin/env node
// Resolve a radio/site name to device IDs + management IPs via a cached
// MyMate device index. The index endpoint always dumps the full catalog
// (~35 MB), so it is fetched ONCE and cached; lookups hit the cache.
// Usage: node scripts/inventory_resolve.mjs <name-fragment> [--refresh]
// Prints: id, name, mgmt_ip, status, site. Status is monitoring state,
// not proof of current reachability. Never paste secrets.
import { loadProjectEnv, loadDeviceIndex } from './mymate_session.mjs';

const wantRefresh = process.argv.includes('--refresh');
const frag = process.argv.slice(2).find((a) => !a.startsWith('--'));
if (!frag) { console.error('Usage: node scripts/inventory_resolve.mjs <name-fragment> [--refresh]'); process.exit(1); }
loadProjectEnv();

const index = await loadDeviceIndex({ refresh: wantRefresh });
const devices = index.devices;
const needle = frag.toLowerCase();
const hits = devices.filter((d) => (d.name || '').toLowerCase().includes(needle) || (d.site || '').toLowerCase().includes(needle));
console.log(JSON.stringify({ fragment: frag, matches: hits.slice(0, 20), totalMatches: hits.length, cacheAge: index.fresh ? 'fresh' : 'cached' }, null, 2));
