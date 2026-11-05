#!/usr/bin/env node
// mymate_query.mjs - one read-only MyMate question per call, compact JSON out.
//
// The curl recipes in the mymate skill take an agent five or more shell steps per question (CSRF cookie, login,
// re-read XSRF, the GET, a python filter), each one a model request. This script logs in, asks, filters and caps
// the answer in one call. Read-only: GET requests only.
//
// Usage:
//   node scripts/mymate_query.mjs site <name-fragment>          devices (server-side search) + open outages for a site
//   node scripts/mymate_query.mjs outages [--match <fragment>] [--closed]
//   node scripts/mymate_query.mjs device <device-id>             live status fields
//   node scripts/mymate_query.mjs ping <device-id> [--hours N]   rtt/loss/jitter summary (default 6 h)
//   node scripts/mymate_query.mjs links <device-id>              topology links touching the device
// Status is MyMate's monitoring state, not proof of reachability. Output never carries credentials.
import { pathToFileURL } from 'node:url';
import { loadProjectEnv, login, apiGet, searchDevices } from './mymate_session.mjs';

const CAP = 15;
const DEVICE_FIELDS = ['id', 'name', 'status', 'mgmt_ip', 'site_id', 'site_name', 'device_type', 'vendor', 'model',
  'poll_method', 'monitored', 'rtt_ms', 'loss_pct', 'ping_at', 'signal_dbm', 'snr_db', 'ccq_pct', 'wireless_clients',
  'freq_mhz', 'cpu_pct', 'mem_used_pct', 'temp_c', 'uptime_seconds', 'last_change', 'discovery_error'];
const OUTAGE_FIELDS = ['device_id', 'device_name', 'site_name', 'mgmt_ip', 'started_at', 'ended_at', 'duration_s', 'ongoing', 'cause', 'acknowledged', 'is_cpe'];

const pick = (obj, fields) => Object.fromEntries(fields.filter((f) => obj && obj[f] !== undefined).map((f) => [f, obj[f]]));
const has = (s, needle) => String(s || '').toLowerCase().includes(needle);
const rows = (body) => (Array.isArray(body) ? body : (body && Array.isArray(body.data) ? body.data : []));

function argValue(argv, flag) {
  const i = argv.indexOf(flag);
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined;
}

function deviceId(raw) {
  if (!/^\d{1,12}$/.test(String(raw || ''))) throw new Error('device id must be digits');
  return String(raw);
}

export async function run(argv) {
  const [cmd, arg] = argv;
  if (!['site', 'outages', 'device', 'ping', 'links'].includes(cmd)) {
    throw new Error('usage: mymate_query.mjs site|outages|device|ping|links ... (see the header of this file)');
  }
  if (['device', 'ping', 'links'].includes(cmd)) deviceId(arg);
  if (cmd === 'site' && (!arg || arg.startsWith('--'))) throw new Error('site needs a name fragment');
  const session = await login();
  if (cmd === 'site') {
    const needle = arg.toLowerCase();
    const found = await searchDevices(session, arg);
    const devices = found.devices.filter((d) => has(d.name, needle) || has(d.site, needle));
    const outages = rows(await apiGet(session, '/api/outages?state=open')).filter((o) => has(o.site_name, needle) || has(o.device_name, needle));
    return {
      fragment: arg,
      devices: devices.slice(0, CAP),
      devices_total: devices.length,
      devices_down: devices.filter((d) => d.status === 'down').length,
      open_outages: outages.slice(0, CAP).map((o) => pick(o, OUTAGE_FIELDS)),
      open_outages_total: outages.length,
      search_total: found.total,
    };
  }
  if (cmd === 'outages') {
    const state = argv.includes('--closed') ? 'closed' : 'open';
    const match = (argValue(argv, '--match') || '').toLowerCase();
    let list = rows(await apiGet(session, `/api/outages?state=${state}`));
    if (match) list = list.filter((o) => has(o.site_name, match) || has(o.device_name, match));
    return { state, match: match || null, total: list.length, outages: list.slice(0, 30).map((o) => pick(o, OUTAGE_FIELDS)) };
  }
  if (cmd === 'device') {
    const body = await apiGet(session, `/api/devices/${deviceId(arg)}`);
    return pick(body && body.data ? body.data : body, DEVICE_FIELDS);
  }
  if (cmd === 'ping') {
    const hours = Math.min(168, Math.max(1, Number(argValue(argv, '--hours') || 6)));
    const from = new Date(Date.now() - hours * 3600000).toISOString().slice(0, 19);
    const samples = rows(await apiGet(session, `/api/devices/${deviceId(arg)}/ping-samples?from=${encodeURIComponent(from)}`));
    const nums = (k) => samples.map((p) => Number(p[k])).filter((n) => Number.isFinite(n));
    const stat = (k) => {
      const v = nums(k);
      if (!v.length) return null;
      return { min: Math.min(...v), avg: Math.round((v.reduce((a, b) => a + b, 0) / v.length) * 10) / 10, max: Math.max(...v) };
    };
    const lossy = samples.filter((p) => Number(p.loss_pct) > 0);
    return { device_id: Number(arg), hours, samples: samples.length, rtt_ms: stat('rtt_ms'), loss_pct: stat('loss_pct'), jitter_ms: stat('jitter_ms'),
      samples_with_loss: lossy.length, last_ts: samples.length ? samples[samples.length - 1].ts : null };
  }
  if (cmd === 'links') {
    const id = Number(deviceId(arg));
    const links = rows(await apiGet(session, '/api/links', 120000)).filter((l) => Number(l.a_device_id) === id || Number(l.b_device_id) === id);
    return { device_id: id, total: links.length, links: links.slice(0, 30).map((l) => pick(l, ['id', 'a_device_id', 'b_device_id', 'a_interface', 'b_interface', 'bw_a_mbps', 'bw_b_mbps', 'eff_a_mbps', 'eff_b_mbps', 'status'])) };
  }
  throw new Error('usage: mymate_query.mjs site|outages|device|ping|links ... (see the header of this file)');
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  loadProjectEnv();
  run(process.argv.slice(2))
    .then((out) => { console.log(JSON.stringify(out, null, 2)); })
    .catch((err) => { console.error(`mymate_query: ${err.message}`); process.exit(1); });
}
