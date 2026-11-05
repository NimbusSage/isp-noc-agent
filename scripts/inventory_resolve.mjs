#!/usr/bin/env node
// Resolve a radio/site name to device IDs + management IPs with MyMate's
// server-side device search (/api/devices?q=). The device list itself is
// paginated (~37k devices), so a lookup never pulls the catalogue.
// Usage: node scripts/inventory_resolve.mjs <name-fragment> [--refresh]
// Prints: id, name, mgmt_ip, status, site. Status is monitoring state,
// not proof of current reachability. Never paste secrets.
import { loadProjectEnv, login, searchDevices, loadDeviceIndex } from './mymate_session.mjs';

const wantRefresh = process.argv.includes('--refresh');
const frag = process.argv.slice(2).find((a) => !a.startsWith('--'));
if (!frag) { console.error('Usage: node scripts/inventory_resolve.mjs <name-fragment> [--refresh]'); process.exit(1); }
loadProjectEnv();

// The lookup is a server-side search. --refresh also rebuilds the full index cache that fleet_bind_sweep.mjs reads.
const session = await login();
if (wantRefresh) await loadDeviceIndex({ refresh: true, session });
const { devices } = await searchDevices(session, frag);
const needle = frag.toLowerCase();
const hits = devices.filter((d) => (d.name || '').toLowerCase().includes(needle) || (d.site || '').toLowerCase().includes(needle));
console.log(JSON.stringify({ fragment: frag, matches: hits.slice(0, 20), totalMatches: hits.length, source: 'search' }, null, 2));
