// Tests for scripts/mymate_query.mjs and the shared session helpers against a local fake MyMate (loopback only).
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(here, 'mymate_query.mjs');
const USER = 'nocagent-test';
const PASS = `pw-${Math.random().toString(36).slice(2)}`;

function fakeMyMate() {
  const seen = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers });
      const json = (code, obj, headers = {}) => { res.writeHead(code, { 'Content-Type': 'application/json', ...headers }); res.end(JSON.stringify(obj)); };
      if (req.url === '/sanctum/csrf-cookie') return json(204, {}, { 'Set-Cookie': ['XSRF-TOKEN=tok%3D1; Path=/', 'mymate_session=pre; Path=/'] });
      if (req.url === '/login' && req.method === 'POST') {
        const creds = JSON.parse(body || '{}');
        if (creds.email !== USER || creds.password !== PASS || req.headers['x-xsrf-token'] !== 'tok=1') return json(422, { message: 'bad' });
        return json(200, {}, { 'Set-Cookie': ['mymate_session=good; Path=/'] });
      }
      if (!String(req.headers.cookie || '').includes('mymate_session=good') || req.headers.origin === undefined) return json(401, { message: 'Unauthenticated.' });
      if (req.method !== 'GET') return json(405, {});
      if (req.url.startsWith('/api/devices?')) {
        const u = new URL(req.url, 'http://x');
        const all = [
          { id: 1, name: 'golf-ap-5n', mgmt_ip: '<ip-a>', status: 'up', site_name: 'Golf', extra: 'x' },
          { id: 2, name: 'golf-bh', mgmt_ip: '<ip-b>', status: 'down', site_name: 'Golf' },
          { id: 3, name: 'hotel-ap', mgmt_ip: '<ip-c>', status: 'up', site_name: 'Hotel' },
        ];
        const q = (u.searchParams.get('q') || '').toLowerCase();
        const rows = q ? all.filter((d) => d.name.includes(q) || d.site_name.toLowerCase().includes(q)) : all;
        const per = 2;
        const page = Number(u.searchParams.get('page') || 1);
        return json(200, { data: rows.slice((page - 1) * per, page * per), meta: { total: rows.length, last_page: Math.ceil(rows.length / per) || 1, current_page: page } });
      }
      if (req.url === '/api/devices-unused') return json(200, { data: [
        { id: 1, name: 'golf-ap-5n', mgmt_ip: '<ip-a>', status: 'up', site_name: 'Golf', extra: 'x' },
        { id: 2, name: 'golf-bh', mgmt_ip: '<ip-b>', status: 'down', site_name: 'Golf' },
        { id: 3, name: 'hotel-ap', mgmt_ip: '<ip-c>', status: 'up', site_name: 'Hotel' },
      ] });
      if (req.url === '/api/outages?state=open') return json(200, { data: [
        { device_id: 2, device_name: 'golf-bh', site_name: 'Golf', started_at: 't', ongoing: true, secret_field: 'no' },
        { device_id: 9, device_name: 'india-sw', site_name: 'India', started_at: 't', ongoing: true },
      ] });
      if (req.url === '/api/devices/2') return json(200, { data: { id: 2, name: 'golf-bh', status: 'down', rtt_ms: null, internal_only: 'not-for-output' } });
      if (req.url.startsWith('/api/devices/2/ping-samples?from=')) return json(200, { data: [
        { ts: 'a', rtt_ms: 10, loss_pct: 0, jitter_ms: 1 }, { ts: 'b', rtt_ms: 30, loss_pct: 50, jitter_ms: 3 },
      ] });
      if (req.url === '/api/links') return json(200, { data: [
        { id: 7, a_device_id: 2, b_device_id: 3, bw_a_mbps: 1000 }, { id: 8, a_device_id: 5, b_device_id: 6 },
      ] });
      return json(404, {});
    });
  });
  return { server, seen };
}

// Async on purpose: the fake server lives in this process, so a synchronous spawn would block it.
function runNode(args, env) {
  return runNodeScript(SCRIPT, args, env);
}

function runNodeScript(script, args, env) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [script, ...args], { env });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (c) => { stdout += c; });
    child.stderr.on('data', (c) => { stderr += c; });
    const t = setTimeout(() => child.kill('SIGKILL'), 20000);
    child.on('close', (status) => { clearTimeout(t); resolve({ status, stdout, stderr }); });
  });
}

async function withServer(fn) {
  const { server, seen } = fakeMyMate();
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mymate-q-'));
  const env = {
    PATH: process.env.PATH, HOME: dir, MYMATE_URL: `http://127.0.0.1:${server.address().port}`, MYMATE_USER: USER, MYMATE_PASS: PASS,
    NOC_AGENT_LOG_DIR: dir, NOC_DOTENV: path.join(dir, 'absent.env'), NOC_PROFILE_FILE: path.join(dir, 'absent.json'),
  };
  const q = (...args) => runNode(args, env);
  try { await fn({ q, seen, dir, env }); } finally { server.close(); fs.rmSync(dir, { recursive: true, force: true }); }
}

test('site: devices from the server-side search (all pages) plus matching open outages, in one call', async () => {
  await withServer(async ({ q, seen }) => {
    const r = await q('site', 'golf');
    assert.equal(r.status, 0, r.stderr);
    const out = JSON.parse(r.stdout);
    assert.equal(out.devices_total, 2);
    assert.equal(out.devices_down, 1);
    assert.deepEqual(out.devices.map((d) => d.id), [1, 2]);
    assert.equal(out.devices[0].extra, undefined, 'rows keep only id/name/mgmt_ip/status/site');
    assert.equal(out.open_outages_total, 1);
    assert.equal(out.open_outages[0].secret_field, undefined);
    assert.ok(seen.some((s) => /^\/api\/devices\?q=golf&per_page=100&page=1$/.test(s.url)));
    assert.ok(!seen.some((s) => s.url === '/api/devices'), 'never the whole catalogue');
    assert.ok(seen.every((s) => s.method === 'GET' || s.url === '/login'), 'read-only apart from the login');
  });
});

test('the full index (fleet scripts) walks every page and marks itself complete', async () => {
  await withServer(async ({ env, dir }) => {
    const r = await runNodeScript(path.join(here, 'inventory_resolve.mjs'), ['hotel', '--refresh'], env);
    assert.equal(r.status, 0, r.stderr);
    assert.deepEqual(JSON.parse(r.stdout).matches.map((d) => d.id), [3]);
    const cache = JSON.parse(fs.readFileSync(path.join(dir, 'mymate-device-index.json'), 'utf8'));
    assert.equal(cache.complete, true);
    assert.deepEqual(cache.devices.map((d) => d.id), [1, 2, 3]);
  });
});

test('device, ping, links and outages are compact; output never carries credentials or unknown fields', async () => {
  await withServer(async ({ q }) => {
    const dev = JSON.parse((await q('device', '2')).stdout);
    assert.deepEqual(dev, { id: 2, name: 'golf-bh', status: 'down', rtt_ms: null });
    const ping = JSON.parse((await q('ping', '2', '--hours', '3')).stdout);
    assert.deepEqual([ping.samples, ping.rtt_ms.avg, ping.loss_pct.max, ping.samples_with_loss, ping.hours], [2, 20, 50, 1, 3]);
    const links = JSON.parse((await q('links', '2')).stdout);
    assert.deepEqual(links.links.map((l) => l.id), [7]);
    const out = JSON.parse((await q('outages', '--match', 'india')).stdout);
    assert.equal(out.total, 1);
    for (const r of [await q('device', '2'), await q('ping', '2'), await q('site', 'golf')]) {
      assert.ok(!r.stdout.includes(PASS) && !r.stderr.includes(PASS));
      assert.ok(!r.stdout.includes('not-for-output'));
    }
  });
});

test('bad input and a failed login exit 1 with a short message', async () => {
  await withServer(async ({ q, env }) => {
    assert.match((await q('device', '2; rm -rf /')).stderr, /device id must be digits/);
    assert.match((await q('nope')).stderr, /usage/);
    const bad = await runNode(['device', '2'], { ...env, MYMATE_PASS: 'wrong' });
    assert.equal(bad.status, 1);
    assert.match(bad.stderr, /login failed: HTTP 422/);
    assert.ok(!bad.stderr.includes('wrong'));
  });
});
