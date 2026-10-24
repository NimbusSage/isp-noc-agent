#!/usr/bin/env node
// wave_gui.mjs — Wave radio web UI driver (puppeteer-core + system Chrome).
// Probe/read by default; enable-ssh mutates only with --execute. Credentials
// come from env (WAVE_GUI_USER / WAVE_GUI_PASS) and are never printed.
//
// usage:
//   WAVE_GUI_USER=… WAVE_GUI_PASS=… node scripts/wave_gui.mjs probe --url https://H [--click "Text"]… [--session NAME]
//   WAVE_GUI_USER=… WAVE_GUI_PASS=… node scripts/wave_gui.mjs enable-ssh --url https://H [--click …] --execute
//   node scripts/wave_gui.mjs read --url https://H          # dashboard stats via the gen3 v1.0 API (no browser)
//   node scripts/wave_gui.mjs speedtest --url https://H [--mac XX] [--duration 30] [--direction rx|tx]
// Screenshots → logs/gui/<session>/*.png, element map → elements-N.txt
//
// Element map format: BUTTON|id|TYPE[ CHECKED]|x,y|text (x,y = viewport coords).
//
// Hardening notes:
//   * --session must be a plain name ([A-Za-z0-9._-]); it becomes a directory under
//     logs/gui/, so "../" style values are refused.
//   * --url / WAVE_DEVICE_URL must be http(s)://host[:port] with no credentials or odd schemes.
//   * WAVE_TOKEN_OUT is created 0600 at creation time (never world-readable, even briefly).
//   * CHROME_PATH selects the browser (default /usr/bin/google-chrome). Chrome's sandbox is
//     only disabled when running as root (it cannot start otherwise) or CHROME_NO_SANDBOX=1.
//   * --ignore-certificate-errors stays: radios ship self-signed certificates.

import puppeteer from 'puppeteer-core';
import https from 'node:https';
import http from 'node:http';
import { closeSync, constants, fchmodSync, mkdirSync, openSync, writeFileSync, writeSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { loadStore, poolCredentials } from './cred_store.mjs';

// Used by the v1.0 API mode (read / speedtest) and defined here because main()'s sync
// phase runs before the module tail is evaluated.
const API_BASE = '/api/v1.0';
const kbpsToMbps = (v) => (typeof v === 'number' && isFinite(v)) ? Math.round(v) / 1000 : v ?? null;

const args = process.argv.slice(2);
const cmd = args[0];

function allArgs(flag) {
  const out = [];
  const idx = args.indexOf(flag);
  if (idx >= 0) for (let i = idx; i < args.length; i++) {
    if (args[i].startsWith('--') && args[i] !== flag) break;
    if (args[i] === flag) continue;
    if (args[i] === 'true' || args[i] === 'false') continue;
    out.push(args[i]);
  }
  return out;
}
const argval = (flag) => allArgs(flag)[0] ?? null;

const URL0 = argval('--url') || process.env.WAVE_DEVICE_URL || '';
const EXECUTE = args.includes('--execute');
const SESSION = argval('--session') || `gui-${Date.now()}`;
if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(SESSION)) { console.error('--session must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}'); process.exit(2); }
const CLICKS = allArgs('--click');
const CLICKS_XY = allArgs('--clickxy');
const SHOT_DIR = path.join(process.cwd(), 'logs', 'gui', SESSION);
mkdirSync(SHOT_DIR, { recursive: true });

const USER = process.env.WAVE_GUI_USER;
const PASS = process.env.WAVE_GUI_PASS;
if (!USER || !PASS) {
  // read/speedtest fall back to the credstore ubnt-wave pool (see poolCredentials below);
  // only the browser flows strictly require the explicit env pair.
  if (['probe', 'enable-ssh'].includes(cmd)) { console.error('WAVE_GUI_USER / WAVE_GUI_PASS env required'); process.exit(2); }
}
if (!URL0) { console.error('device URL required: pass --url https://<radio-ip> (or set WAVE_DEVICE_URL)'); process.exit(2); }
try {
  const u = new URL(URL0);
  if (!['http:', 'https:'].includes(u.protocol) || !u.hostname || u.username || u.password) throw new Error('bad');
} catch { console.error('--url must be http(s)://host[:port] without embedded credentials'); process.exit(2); }

// Where stored credentials may be sent: the same host policy the MCP server applies to its read tools
// (private/loopback space unless NOC_READ_ALLOWED_HOSTS says otherwise). Without it, an agent steered by
// hostile ticket text could aim the whole ubnt-wave credential pool at a host the operator does not own.
// The policy lives in the compiled MCP server; if it is not built, refuse rather than skip the check.
{
  let policy;
  try {
    const here = path.dirname(fileURLToPath(import.meta.url));
    policy = await import(pathToFileURL(path.join(here, '..', 'mcp-server', 'dist', 'security', 'hosts.js')).href);
  } catch {
    console.error('host policy unavailable: build the MCP server first (cd mcp-server && npm ci && npm run build)');
    process.exit(2);
  }
  const hostname = new URL(URL0).hostname.replace(/^\[|\]$/g, '');
  const verdict = policy.checkReadHost(hostname, process.env);
  if (!verdict.ok) { console.error(`refusing ${hostname}: ${verdict.error}`); process.exit(2); }
}

/** Write the captured session token with mode 0600 from the first byte. */
function writeTokenFile(file, token) {
  const fd = openSync(file, constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC | (constants.O_NOFOLLOW || 0), 0o600);
  try { fchmodSync(fd, 0o600); writeSync(fd, token || ''); } finally { closeSync(fd); }
}

const log = (...a) => console.log(...a);

async function shot(page, name) {
  await page.screenshot({ path: path.join(SHOT_DIR, `${name}.png`), fullPage: true });
}

async function snapInteractive(page) {
  return page.evaluate(() => {
    const sel = 'button, a, input, select, [role=switch], [role=button], [role=tab], [onclick], [aria-label], svg, label, span, h1, h2, h3';
    const out = [];
    for (const el of [...document.querySelectorAll(sel)]) {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      const tag = el.tagName.toLowerCase();
      const id = el.id || '';
      const type = el.getAttribute('type') || '';
      const aria = el.getAttribute('aria-label') || '';
      const txt = (el.innerText || el.value || el.placeholder || aria || '').trim().replace(/\s+/g, ' ').slice(0, 70);
      const checked = el.checked === true || el.getAttribute('aria-checked') === 'true' ? ' CHECKED' : '';
      const disabled = el.disabled ? ' DISABLED' : '';
      const xy = `${Math.round(r.x + r.width / 2)},${Math.round(r.y + r.height / 2)}`;
      const isCtl = ['button', 'a', 'input', 'select', 'label', 'svg'].includes(tag) || r.width < 60;
      if (!txt && !isCtl) continue;
      out.push(`${tag}|${id}|${type}${checked}${disabled}${aria ? ' aria=' + aria.slice(0, 30) : ''}|${xy}|${txt}`);
    }
    return [...new Set(out)];
  });
}

async function dump(page, n) {
  const items = await snapInteractive(page);
  writeFileSync(path.join(SHOT_DIR, `elements-${n}.txt`), items.join('\n'));
  log(`element map: ${items.length} interactive elements → elements-${n}.txt (${n} shot)`);
  return items;
}

async function main() {
  if (['read', 'speedtest'].includes(cmd)) {
    const out = cmd === 'read'
      ? await apiRead()
      : await apiSpeedtest(argval('--mac'), argval('--direction') || 'both', Math.max(1, Math.min(120, Number(argval('--duration') || 30) || 30)));
    console.log(JSON.stringify(out));
    return;
  }
  if (!['probe', 'enable-ssh'].includes(cmd)) { console.error('unknown command:', cmd); process.exit(2); }
  if (cmd === 'enable-ssh' && !EXECUTE) log('DRY-RUN: add --execute to actually change settings');

  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome',
    headless: 'new',
    args: [
      ...((process.env.CHROME_NO_SANDBOX === '1' || (process.env.CHROME_NO_SANDBOX !== '0' && process.getuid?.() === 0)) ? ['--no-sandbox'] : []),
      '--ignore-certificate-errors',
    ],
    protocolTimeout: 90_000,
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1400, height: 950 });
    page.setDefaultTimeout(30_000);
    page.setDefaultNavigationTimeout(30_000);

    log('open', URL0.replace(/^https?:\/\//, ''));
    let n = 0;
    await page.goto(URL0, { waitUntil: 'domcontentloaded', ignoreHTTPSErrors: true });
    await new Promise(r => setTimeout(r, 2500));

    const pwds = await page.$$('input[type=password]');
    if (pwds.length) {
      await shot(page, `${++n}-login`);
      // The Wave UI re-renders the login DOM after the first interaction, so
      // remote element handles go stale mid-type ("Node is detached from
      // document", seen live on an MLO5). Do the whole login
      // inside one page.evaluate() — no cross-context handles, no staleness.
      const login = await page.evaluate((user, pass) => {
        const inputs = [...document.querySelectorAll('input')];
        const userEl = inputs.find(e => {
          const t = (e.getAttribute('type') || 'text').toLowerCase();
          return t !== 'password' && t !== 'checkbox' && t !== 'radio' && t !== 'hidden' && t !== 'submit';
        });
        const passEl = inputs.find(e => e.getAttribute('type') === 'password');
        if (!passEl) return 'no-password-field';
        const set = (el, v) => {
          const proto = Object.getPrototypeOf(el);
          const s = Object.getOwnPropertyDescriptor(proto, 'value').set;
          s.call(el, v);
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        };
        if (userEl) set(userEl, user);
        set(passEl, pass);
        const btn = [...document.querySelectorAll('button, input[type=submit], [type=submit], a, [role=button]')]
          .find(b => /log ?in|sign ?in|login|submit/i.test(b.innerText || b.value || b.getAttribute('aria-label') || ''));
        if (btn) { btn.click(); return 'clicked-button'; }
        passEl.focus();
        passEl.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, which: 13, bubbles: true }));
        return 'pressed-enter';
      }, USER, PASS);
      log('login action:', login);
      if (login === 'no-password-field') { await shot(page, `${++n}-form`); throw new Error('login form vanished before typing'); }
      // wait for SPA transition with generous settle time; re-render storm can pause main thread
      await page.waitForFunction(() => !document.querySelector('input[type=password]'), { timeout: 15000, polling: 700 }).catch(() => null);
      await new Promise(r => setTimeout(r, 3500));
      await shot(page, `${++n}-after-login`);
      if ((await page.$$('input[type=password]')).length) throw new Error('login failed (password field still present)');
      log('login OK');
    }

    if (process.env.WAVE_TOKEN_OUT) {
      // capture session bearer token for API use (written to file, never stdout)
      const tok = await page.evaluate(() => sessionStorage.getItem('x-auth-token') || sessionStorage.getItem('auth-token') || '');
      writeTokenFile(process.env.WAVE_TOKEN_OUT, tok);
      log('token captured len=' + tok.length);
    }

    n += 1;
    await shot(page, `${n}-${'dashboard'}`);
    let items = await dump(page, n);

    for (const c of CLICKS_XY) {
      const [x, y] = c.split(',').map(Number);
      log(`clickxy: ${x},${y}`);
      await page.mouse.click(x, y);
      await new Promise(r => setTimeout(r, 3000));
      n += 1;
      await shot(page, `${n}-xy${x}_${y}`);
      items = await dump(page, n);
      const sshHits = items.filter(t => /ssh/i.test(t));
      if (sshHits.length) log(`SSH-FOUND after xy ${x},${y}:`, sshHits.slice(0, 6));
    }

    for (const c of CLICKS) {
      const clickStart = Date.now();
      log(`click: "${c}"`);
      const el = await page.evaluateHandle((txt) => {
        const norm = s => (s || '').trim().toLowerCase();
        return [...document.querySelectorAll('button, a, [role=button], [role=switch], [role=tab], input[type=submit], [onclick], label, span')]
          .find(e => {
            const r = e.getBoundingClientRect();
            if (r.width < 1 || r.height < 1) return false;
            return norm(e.innerText) === norm(txt) || norm(e.getAttribute('aria-label')) === norm(txt);
          });
      }, c);
      const hnd = el.asElement();
      if (!hnd) { log(`CLICK-MISS: no element with text "${c}"`); break; }
      await hnd.click();
      await new Promise(r => setTimeout(r, 3000));
      n += 1;
      await shot(page, `${n}-after-${c.replace(/[^a-z0-9]+/gi, '_')}`.slice(0, 40));
      items = await dump(page, n);
    }
    if (cmd === 'enable-ssh') {
      // Wave UI settings page: 4th sidebar icon at (28,238) from the FRESH-LOGIN dashboard,
      // section card "SSH Server", checkbox id=sshServer.enabled, sticky Save Changes
      // button (disabled until dirty). Verified by per-icon walk (icon y=238
      // is the only page landing with #sshServer.enabled). Sequence = dashboard shot break
      // → fresh login → one click → assertion, never multiple GUI sessions chained.
      const SEQUENCED = CLICKS_XY.length > 0;
      if (SEQUENCED) {
        log('sequenced run: nav clicks already applied, going straight to the checkbox flow');
      } else {
        // Fast break out of the dashboard dump phase: nothing to do before nav.
        log('enable-ssh: fresh-login nav click 28,238');
      }
      await page.mouse.click(28, 238);
      await new Promise(r => setTimeout(r, 3500));
      const CB_SEL = '#sshServer\\.enabled'; // attribute-dot must be escaped in CSS
      const cb = await page.$(CB_SEL);
      if (!cb) {
        const last = await dump(page, ++n);
        await shot(page, `${++n}-enable-verify-fail`);
        const near = (await snapInteractive(page)).filter(t => /20[0-9]|2[0-9]{2},/i.test(t)).length;
        throw new Error(`sshServer.enabled checkbox not found on settings page (rows=${last.length}, sidebar-ish=${near}, session=${SHOT_DIR})`);
      }
      const state = await cb.evaluate(el => ({ checked: el.checked }));
      log('sshServer.enabled currently:', state.checked);
      if (state.checked) { log('SSH already enabled — nothing to do'); return; }
      if (!EXECUTE) { log('DRY-RUN: would enable SSH and click Save Changes. Re-run with --execute.'); return; }

      const toggledVia = await page.evaluate(async (sel) => {
        const poll = (cond, tries, ms) => new Promise(res => { let t = 0; const iv = setInterval(() => { if (cond() || ++t >= tries) { clearInterval(iv); res(cond()); } }, ms); });
        const el = document.querySelector(sel);
        if (!el) return 'GONE?';
        const before = el.checked;
        // try wrapper click first (custom-styled toggles often swallow raw input clicks)
        const wrap = el.closest('label, [class*=checkbox], [class*=toggle], [class*=switch]') || el;
        wrap.click();
        let ok = await poll(() => el.checked !== before, 6, 400);
        let via = ok ? 'wrapper-click' : null;
        if (!ok) {
          el.click();
          ok = await poll(() => el.checked !== before, 4, 350);
          via = ok ? 'input-click' : null;
        }
        if (!ok) {
          // framework-visible synthetic toggle (Vue v-model listens for input/change)
          el.checked = !before;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          ok = await poll(() => el.checked, 4, 300);
          via = ok ? 'synthetic-change' : 'FAILED';
        }
        return `${via}|before=${before}|after=${el.checked}|html=${el.outerHTML.slice(0, 160)}`;
      }, CB_SEL);
      log('toggle result:', toggledVia);
      if (toggledVia.startsWith('FAILED')) { await dump(page, ++n); throw new Error('checkbox never toggled true — see dump'); }

      // poll for the dirty-state Save Changes button to go live (may lag the toggle)
      const saveReady = await page.waitForFunction(() => {
        const b = [...document.querySelectorAll('button')].find(x => /save changes/i.test(x.innerText || ''));
        return b && !b.disabled ? b : false;
      }, { timeout: 8000, polling: 500 }).catch(() => null);
      const saveBtn = saveReady && saveReady.asElement ? saveReady.asElement() : null;
      if (!saveBtn) { await dump(page, ++n); throw new Error('Save Changes never became enabled after toggle — see dump'); }
      await shot(page, `${++n}-before-save`);
      await saveBtn.click();
      await new Promise(r => setTimeout(r, 5000));
      await shot(page, `${++n}-after-save`);
      const after = await (await page.$(CB_SEL)).evaluate(el => el.checked);
      log('sshServer.enabled after save:', after);
      if (!after) { await shot(page, `${++n}-verify-fail`); throw new Error('checkbox shows unchecked after save — verify manually'); }
      log('SSH-ENABLE OK (UI verified); TCP banner still needs SSH port check');
      return;
    }
    log('probe complete; session', SESSION);
    if (cmd === 'probe' && process.env.WAVE_TOKEN_OUT) {
      // capture session bearer token for API use (written to file, never stdout)
      const tok = await page.evaluate(() => sessionStorage.getItem('x-auth-token') || sessionStorage.getItem('auth-token') || '');
      writeTokenFile(process.env.WAVE_TOKEN_OUT, tok);
      log('token captured to', process.env.WAVE_TOKEN_OUT, 'len=' + tok.length);
    }
  } finally {
    await browser.close();
  }
}

main().catch(e => { console.error('GUI-FAIL:', e.message); process.exit(1); });

// =========================== v1.0 UI API mode (read / speedtest) ==========================
// Verified live 2026-10-24 on Wave Pro firmware MGMP.ipq807x.v4.1.1 (anonymized 60 GHz link pair):
//   login   POST /api/v1.0/user/login {username,password} → 200 with a 40-char x-auth-token
//           response header (the legacy /api/auth route 401s for unauthenticated callers and
//           is NOT the gen3 login path)
//   stats   GET /api/v1.0/statistics    → [{wireless:{linkQuality,radios,peers,carrierDrop}}]
//           capacity numbers are kbps (verified: combined 1751750 = 1.75G on a live radio)
//   device  GET /api/v1.0/device        → {identification:{model,product,firmware,mac}}
//   speed   POST /api/v1.0/tools/airos/speedtest/start {mac,duration, direction: rx|tx|dx}
//           (direction: rx, tx or dx per the API's own error text; the modal default label
//            "all" is NOT accepted by the endpoint)
//           GET  /api/v1.0/tools/airos/speedtest → {status: running|completed,...}
//           POST /api/v1.0/tools/airos/speedtest/stop {mac}
//           speed-test numbers are Mbps (verified 664.38 avg_rx on the same radio)
// pool in priority order (scripts/cred_store.mjs; passphrase = NOC_MCP_PASSPHRASE env or
// repo .env). Secrets never printed/logged; tokens only inside the process.

function guiCreds() {
  if (USER && PASS) return [{ name: 'env', username: USER, password: PASS, priority: 100 }];
  try {
    const store = loadStore();
    return (poolCredentials(store, 'ubnt-wave') || []).map((c, i) => ({
      name: c.name || `pool-${i}`,
      username: String(c.username || 'ubnt').trim() || 'ubnt',
      password: String(c.password || ''),
      priority: c.priority ?? i,
    })).filter(c => c.password);
  } catch (e) {
    console.error('WAVE_GUI_USER/WAVE_GUI_PASS not set and the ubnt-wave credstore pool is unavailable: ' + e.message);
    process.exit(2);
  }
}

function apiRequest(rawUrl, method, headers = {}, data = null, timeoutMs = 20000) {
  const u = new URL(rawUrl);
  const mod = u.protocol === 'https:' ? https : http;
  const payload = data == null ? null : Buffer.from(JSON.stringify(data));
  const hostHeader = u.port ? `${u.hostname}:${u.port}` : u.hostname;
  const opts = {
    method,
    host: u.hostname,
    port: u.port,
    path: u.pathname + u.search,
    // Radios ship self-signed certificates (matches the browser --ignore-certificate-errors).
    rejectUnauthorized: false,
    timeout: timeoutMs,
    headers: {
      Host: hostHeader,
      Accept: 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
      ...(payload ? { 'Content-Type': 'application/json', 'Content-Length': payload.length } : {}),
      ...headers,
    },
  };
  return new Promise((resolve, reject) => {
    const req = mod.request(opts, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers || {}, body }));
    });
    req.on('timeout', () => req.destroy(new Error('request timed out')));
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

async function apiLoginLogin(base, creds, timeoutMs) {
  // Try each pool credential in priority order; stop on the first HTTP 200 login.
  const errors = [];
  for (const cred of creds) {
    const { status, headers } = await apiRequest(`${base}${API_BASE}/user/login`, 'POST', {},
      { username: cred.username, password: cred.password }, timeoutMs);
    const token = headers['x-auth-token'] || '';
    if (status === 200 && token) return { token, credName: cred.name, username: cred.username };
    if (status === 401 || status === 403) { errors.push(`${cred.name}:HTTP ${status}`); continue; }
    throw new Error(`login to ${base} returned HTTP ${status}`);
  }
  throw new Error(`web UI login failed against ${creds.length} credential(s) (${errors.join(', ')}) — the gen3 web password may differ from the SSH pool`);
}

async function apiGet(base, token, path, timeoutMs = 20000) {
  const r = await apiRequest(`${base}${API_BASE}${path}`, 'GET', { 'x-auth-token': token }, null, timeoutMs);
  if (r.status === 401 || r.status === 403) throw new Error(`HTTP ${r.status} on ${path} (= session expired; re-run)`);
  if (r.status !== 200) throw new Error(`HTTP ${r.status} on ${path}`);
  try { return JSON.parse(r.body); } catch { throw new Error(`non-JSON response on ${path}: ${r.body.slice(0, 120)}`); }
}

/** Lowest nonzero histogram bin in dBm (bin width = span/(bins-1)); null when empty. */
function decodeFloorDbm(sig) {
  if (!sig || !Array.isArray(sig.histogram) || typeof sig.minSignal !== 'number') return null;
  const bins = sig.histogram;
  const firstIdx = bins.findIndex((v) => typeof v === 'number' && v > 0);
  if (firstIdx < 0) return null;
  const span = typeof sig.maxSignal === 'number' ? (sig.maxSignal - sig.minSignal) : (bins.length - 1);
  const width = bins.length > 1 ? span / (bins.length - 1) : 0;
  return Math.round((sig.minSignal + width * firstIdx) * 10) / 10;
}

function peerShape(peer, remotePeer) {
  if (!peer) return null;
  const q = peer.linkQuality || {};
  const m = q.mcs || {};
  const sig = q.signalHistogram || {};
  const cap = q.capacity || {};
  return {
    id: peer.id,
    connected: !!peer.connected,
    linkState: peer.linkState ?? null,
    connectionTimeMs: peer.connectionTime ?? null,
    signalDbm: q.signal ?? null,
    signalDayDbm: q.signalDay ?? null,
    idealSignalDbm: q.idealSignal ?? null,
    minSignalDbm: sig.minSignal ?? null,
    maxSignalDbm: sig.maxSignal ?? null,
    // Lowest signal bin the radio actually decoded frames at (empirical survival
    // floor). margin = signalDayDbm - decodeFloorDbm feeds the rain-fade model.
    decodeFloorDbm: decodeFloorDbm(sig),
    mcs: {
      rxIdx: m.rxIdx ?? null, txIdx: m.txIdx ?? null,
      rxIdxIdeal: m.rxIdxIdeal ?? null, txIdxIdeal: m.txIdxIdeal ?? null,
      rxLabel: m.rxLabel ?? null, txLabel: m.txLabel ?? null,
    },
    capacityMbps: {
      combined: kbpsToMbps(cap.combined), combinedIdeal: kbpsToMbps(cap.combinedIdeal),
      dl: kbpsToMbps(cap.dl), dlIdeal: kbpsToMbps(cap.dlIdeal),
      ul: kbpsToMbps(cap.ul), ulIdeal: kbpsToMbps(cap.ulIdeal),
    },
    linkScore: q.linkScore ?? null,
    airtimePct: q.airtime ? { dl: q.airtime.dl ?? null, ul: q.airtime.ul ?? null } : null,
    gps: (peer && peer.gps) ? { lat: peer.gps.lat ?? null, lon: peer.gps.lon ?? null, altM: peer.gps.alt ?? null }
      : (remotePeer && remotePeer.gps ? { lat: remotePeer.gps.lat ?? null, lon: remotePeer.gps.lon ?? null, altM: remotePeer.gps.alt ?? null } : null),
  };
}

function radioShape(r) {
  if (!r) return null;
  return {
    id: r.id ?? null,
    linkState: r.linkState ?? null,
    frequencyCenterMhz: r.frequency ? (r.frequency.center ?? null) : null,
    channelWidthTxMhz: r.channelWidth ? (r.channelWidth.tx ?? null) : null,
    waveAi: r.waveAi ?? null,
    serviceUptimeMs: r.serviceUptime ?? null,
    serviceDowntimeMs: r.serviceDowntime ?? null,
    conductedTxDbm: r.outputPower ? (r.outputPower.conducted ?? null) : null,
    eirpDbm: r.outputPower ? (r.outputPower.eirp ?? null) : null,
    antennaName: r.antenna ? (r.antenna.name ?? null) : null,
    antennaGainDbi: r.antenna ? (r.antenna.gain ?? null) : null,
  };
}

async function apiRead() {
  const base = URL0.replace(/\/$/, '');
  const creds = guiCreds();
  const { token, credName, username } = await apiLoginLogin(base, creds, 20000);
  const [stats, device] = await Promise.all([
    apiGet(base, token, '/statistics'),
    apiGet(base, token, '/device'),
  ]);
  const w = (stats && stats[0] && stats[0].wireless) || {};
  const radios = w.radios || [];
  const peers = w.peers || [];
  const ownMac = (device.identification && device.identification.mac) || '';
  // Peer MAC: everything MAC-looking inside peers, minus this device's own MAC.
  const macRe = /[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}/g;
  const peerMacs = [...new Set(String(JSON.stringify(peers)).match(macRe) || [])].filter(m => m.toLowerCase() !== ownMac.toLowerCase());
  const radioState = (id) => {
    const r = radios.find((x) => x && x.id === id);
    if (!r || !peers.length || !peers[0]) return null;
    const localPeer = (peers[0].local || []).find((p) => p && p.id === id);
    const remotePeer = (peers[0].remote || []).find((p) => p && p.id === id);
    // localPeer.gps (when present) is this radio's own fix; the remote peer entry
    // carries the far radio's fix. Both are kept so callers can haversine the path length.
    return { ...radioShape(r), localPeer: peerShape(localPeer, remotePeer), remotePeer: peerShape(remotePeer) };
  };
  return {
    mode: 'read',
    url: base,
    loginCredName: credName,
    loginUsername: username,
    device: {
      model: (device.identification && device.identification.model) || null,
      product: (device.identification && device.identification.product) || null,
      firmware: (device.identification && device.identification.firmware) || null,
      mac: ownMac,
      family: (device.identification && device.identification.family) || null,
    },
    radios: { main: radioState('main'), backup: radioState('backup') },
    carrierDrop: { dropped: !!(w.carrierDrop && w.carrierDrop.dropped), reported: !!w.carrierDrop },
    peerMacs,
    timestamp: new Date().toISOString(),
  };
}

async function apiSpeedtest(macArg, direction, duration) {
  if (!Number.isFinite(duration) || duration <= 0 || duration > 600) {
    throw new Error('--duration must be 1..600 seconds');
  }
  if (!['rx', 'tx', 'both'].includes(direction)) throw new Error(`bad --direction ${direction}: rx, tx or both`);
  const base = URL0.replace(/\/$/, '');
  const creds = guiCreds();
  const { token } = await apiLoginLogin(base, creds, 20000);
  // Friendlier diagnostics + stats needed for MAC auto-detection.
  const stats = await apiGet(base, token, '/statistics');
  const device = await apiGet(base, token, '/device');
  const ownMac = (device.identification && device.identification.mac) || '';
  const macRe = /[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}/g;
  let mac = macArg;
  if (!mac) {
    const peers = (stats && stats[0] && stats[0].wireless && stats[0].wireless.peers) || [];
    const found = [...new Set(String(JSON.stringify(peers)).match(macRe) || [])].filter(m => m.toLowerCase() !== ownMac.toLowerCase());
    if (found.length === 1) mac = found[0];
    else if (!found.length) throw new Error('no peer MAC found in statistics — pass --mac manually or check the link');
    else throw new Error(`ambiguous peer MACs (${found.length}) in statistics — pass --mac`);
  } else if (!macRe.test(mac)) throw new Error(`--mac must look like MM:MM:MM:MM:MM:MM, got ${mac}`);

  const state = () => apiGet(base, token, '/tools/airos/speedtest');
  // Clear any leftover test session before starting (endpoint accepts only rx|tx|dx).
  await apiRequest(`${base}${API_BASE}/tools/airos/speedtest/stop`, 'POST', { 'x-auth-token': token }, { mac }, 20000).catch(() => {});

  const runOne = async (dir) => {
    const started = await apiRequest(`${base}${API_BASE}/tools/airos/speedtest/start`, 'POST', { 'x-auth-token': token }, { mac, duration, direction: dir }, 20000);
    if (started.status !== 200) throw new Error(`speedtest/start (${dir}) HTTP ${started.status}: ${started.body.slice(0, 160)}`);
    const t0 = Date.now();
    const deadline = t0 + duration * 1000 + 60000;
    let latest = null;
    for (;;) {
      await new Promise(r => setTimeout(r, 4000));
      const s = await state();
      latest = s;
      if (s.status === 'completed') break;
      if (Date.now() > deadline) { await apiRequest(`${base}${API_BASE}/tools/airos/speedtest/stop`, 'POST', { 'x-auth-token': token }, { mac }, 20000).catch(() => {}); throw new Error(`speedtest (${dir}) did not complete within ${Math.round((deadline - t0) / 1000)}s`); }
    }
    await apiRequest(`${base}${API_BASE}/tools/airos/speedtest/stop`, 'POST', { 'x-auth-token': token }, { mac }, 20000).catch(() => {});
    return {
      direction: dir,
      status: latest.status,
      durationSec: latest.duration ?? null,
      avgMbps: dir === 'rx' ? latest.avg_rx ?? null : latest.avg_tx ?? null,
      maxMbps: dir === 'rx' ? latest.max_rx ?? null : latest.max_tx ?? null,
    };
  };

  const results = [];
  if (direction === 'both') {
    results.push(await runOne('rx'));
    results.push(await runOne('tx'));
  } else {
    results.push(await runOne(direction));
  }
  return {
    mode: 'speedtest',
    url: base,
    device: { model: (device.identification && device.identification.model) || null, mac: ownMac },
    peerMac: mac,
    durationSec: duration,
    results,
    timestamp: new Date().toISOString(),
  };
}
