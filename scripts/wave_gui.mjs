#!/usr/bin/env node
// wave_gui.mjs — Wave radio web UI driver (puppeteer-core + system Chrome).
// Probe/read by default; enable-ssh mutates only with --execute. Credentials
// come from env (WAVE_GUI_USER / WAVE_GUI_PASS) and are never printed.
//
// usage:
//   WAVE_GUI_USER=… WAVE_GUI_PASS=… node scripts/wave_gui.mjs probe --url https://H [--click "Text"]… [--session NAME]
//   WAVE_GUI_USER=… WAVE_GUI_PASS=… node scripts/wave_gui.mjs enable-ssh --url https://H [--click …] --execute
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
import { closeSync, constants, fchmodSync, mkdirSync, openSync, writeFileSync, writeSync } from 'node:fs';
import path from 'node:path';

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
if (!USER || !PASS) { console.error('WAVE_GUI_USER / WAVE_GUI_PASS env required'); process.exit(2); }
if (!URL0) { console.error('device URL required: pass --url https://<radio-ip> (or set WAVE_DEVICE_URL)'); process.exit(2); }
try {
  const u = new URL(URL0);
  if (!['http:', 'https:'].includes(u.protocol) || !u.hostname || u.username || u.password) throw new Error('bad');
} catch { console.error('--url must be http(s)://host[:port] without embedded credentials'); process.exit(2); }

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
