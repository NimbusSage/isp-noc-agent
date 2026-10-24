#!/usr/bin/env node
// mt_mactelnet.mjs — SSH -> near RouterOS -> /tool mac-telnet -> far RouterOS.
//
// THE WAY to reach island routers (temp-site-connection skill): when MyMate
// shows an island dark but MNDP/CDP on the near router still hears island
// MACs, the island is alive behind a dead routed path and mac-telnet over
// the surviving L2 reaches it. Same for post-bridge far-leg work.
//
// Why this helper exists (verified live, INC-F-01):
// - `/tool mac-telnet` over an SSH *exec channel* silently does nothing
//   (1s exit, zero output) — piped stdin is ignored. It requires a real
//   interactive shell (pty), which this helper opens via ssh2.
// - RouterOS pty line discipline needs `\r` line endings; `\n` alone is
//   echoed but never executed.
// - Passwords resolve at runtime from the ENCRYPTED MCP store
//   (mcp-server/config/credentials.enc, repo-relative to this script, passphrase
//   NOC_MCP_PASSPHRASE from the environment or .env) via scripts/cred_store.mjs, or
//   from an explicit NOC_MIKROTIK_SSH_PASSWORD env var (user "admin"). The plaintext
//   credentials.json is NOT read. Every secret is redacted from stdout; only
//   <redacted> ever prints.
// - Host keys are verified (ssh2 hostVerifier, SHA-256 fingerprints kept in
//   ssh2_hostkeys.json next to the known_hosts file): SSH_HOSTKEY_CHECKING=
//   accept-new (default) | yes | no — see scripts/ssh_opts.mjs.
//
// Safety: READ-ONLY by default. Mutating RouterOS verbs
// (set/add/remove/enable/disable/unset/reboot/shutdown) are refused unless
// `--write --incident-id <ID>` is passed, and the session is tagged in the
// local audit line. This preserves the INC-E-01 far-leg OSPF path (explicit)
// while making diagnosis safe by default.
//
// Usage:
//   node scripts/mt_mactelnet.mjs --jump 10.0.0.2 --mac AA:BB:CC:DD:EE:FF \
//     --cmd '/system identity print' --cmd '/routing ospf neighbor print'
// Exit codes: 0 = all commands ran; 1 = auth/transport failure; 2 = usage/guard.
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { loadStore, poolCredentials } from './cred_store.mjs';
import { makeHostVerifier } from './ssh_opts.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

// ssh2 is LAZY-LOADED: the pure helpers (isReadOnly, redactSecrets) run in
// `node --test` and any importer without the mcp-server dependency tree — CI
// helper-tests job has no node_modules. Real SSH sessions (only in the CLI
// flow) resolve ssh2 from mcp-server/package.json at call time.
let _Client = null;
function getClient() {
  if (!_Client) {
    const require = createRequire(resolve(ROOT, 'mcp-server', 'package.json'));
    _Client = require('ssh2').Client;
  }
  return _Client;
}

// ---- read-only guard ---------------------------------------------------------
// Heuristic defense in depth (the real control is the explicit --write +
// --incident-id gate and the MCP write gates). It is deliberately a WHITELIST:
// RouterOS accepts abbreviated verbs (`/system reb` == `/system reboot`), so a
// blacklist of verb spellings cannot be sound. A command is read-only only if
//   * it contains no scripting/substitution syntax: `[ ]` `{ }` backtick `$(`
//     or a leading `:` scripting command (`:do`, `:execute`, `:foreach`, `:put [..]`),
//   * and EVERY segment - split on `;`, newline/CR, `&&`, `||` - is a bare
//     `/`, `..`, quit/cancel, or has a full read verb (print, monitor, export,
//     get, ping, traceroute, torch) in its menu-path head and no write verb there.
// The old check exempted ANY command containing the substring `print`, so
// "/system reboot ; /ip address print" and "/ip address add comment=print"
// were classed read-only.
const READ_VERBS = new Set(['print', 'monitor', 'export', 'get', 'ping', 'traceroute', 'torch']);
const WRITE_VERBS = new Set([
  'set', 'add', 'remove', 'enable', 'disable', 'unset', 'reboot', 'shutdown', 'reset', 'reset-configuration',
  'import', 'edit', 'move', 'comment', 'run', 'execute', 'upgrade', 'install', 'downgrade', 'fetch',
  'mac-telnet', 'telnet', 'ssh', 'sniffer', 'bandwidth-test', 'flood-ping', 'send', 'copy', 'rename',
  'unmanage', 'release', 'renew', 'scan', 'calibrate', 'blink', 'beep', 'cancel-install', 'sync', 'check-installation',
]);
const SEGMENT_SPLIT = /;|\r?\n|\r|&&|\|\|/;
const SCRIPTING = /[\[\]{}`]|\$\(/;

export function isReadOnly(cmd) {
  const raw = String(cmd ?? '');
  if (!raw.trim()) return false; // nothing to validate: refuse rather than guess
  if (SCRIPTING.test(raw)) return false;
  return raw.split(SEGMENT_SPLIT).every(segmentIsReadOnly);
}

function segmentIsReadOnly(segment) {
  const seg = segment.trim().toLowerCase();
  if (!seg) return true;
  if (/^\/*$/.test(seg) || seg === '..' || /^\/?(quit|cancel)$/.test(seg)) return true;
  if (seg.startsWith(':')) return false;
  // Menu-path head = words before the first argument (key=value, ~ match, quoted
  // text) or the `where` filter keyword. Slashes are path separators ("/ip/address/add").
  const head = [];
  for (const tok of seg.split(/[\s/]+/).filter(Boolean)) {
    if (/[=~"']/.test(tok) || tok === 'where') break;
    head.push(tok);
  }
  if (head.some((t) => t.startsWith(':') || WRITE_VERBS.has(t))) return false;
  return head.some((t) => READ_VERBS.has(t));
}
export function redactSecrets(text, secrets) {
  let o = String(text);
  for (const p of secrets) if (p) o = o.split(p).join('<redacted>');
  return o;
}
function loadPoolPasswords(env = process.env) {
  const explicit = env.NOC_MIKROTIK_SSH_PASSWORD;
  if (explicit && (env.NOC_MIKROTIK_SSH_USER || 'admin') === 'admin') return [explicit];
  let store;
  try { store = loadStore(); } catch (e) {
    fail(`${e.message}. Provide NOC_MIKROTIK_SSH_PASSWORD or the encrypted MCP store (credentials.enc + NOC_MCP_PASSPHRASE).`);
  }
  const out = [];
  for (const c of poolCredentials(store, 'mikrotik-ssh')) {
    if ((c.user || c.username) === 'admin' && c.password) out.push(c.password);
  }
  return [...new Set(out)];
}
function arg(name) {
  const i = process.argv.indexOf(`--${name}`);
  if (i < 0) return null;
  if (['write'].includes(name)) return true;
  return process.argv[i + 1] ?? null;
}
function fail(msg) { console.error(`mt_mactelnet: ${msg}`); process.exit(2); }

const SSH_OPTS = {
  port: 22, username: 'admin', readyTimeout: 25000,
  algorithms: { serverHostKey: ['ssh-rsa', 'rsa-sha2-256', 'rsa-sha2-512', 'ecdsa-sha2-nistp256', 'ssh-ed25519'] },
};

const isMain = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  // CLI parsing lives INSIDE the main guard so `node --test` (and any other
  // importer) can load the pure helpers without triggering usage failures.
  const jumpHost = arg('jump');
  const targetMac = arg('mac');
  const cmds = process.argv.filter((a, i) => process.argv[i - 1] === '--cmd');
  const allowWrite = Boolean(arg('write'));
  const incidentId = arg('incident-id');
  if (!jumpHost) fail('--jump <near-router-ip> is required');
  if (!targetMac || !/^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$/.test(targetMac)) fail('--mac <AA:BB:CC:DD:EE:FF> is required');
  if (!cmds.length) fail('at least one --cmd is required');
  if (allowWrite && !incidentId) fail('--write requires --incident-id <ID>');
  for (const c of cmds) {
    if (!allowWrite && !isReadOnly(c)) fail(`refusing mutating command without --write --incident-id: ${c}`);
  }
  const SECRETS = loadPoolPasswords();
  if (!SECRETS.length) fail('no mikrotik-ssh admin passwords in credential store');
  // Outer hop: first pool password that connects (mk.sh order).
  let outerPw = null;
  for (const pw of SECRETS) {
    const probe = await new Promise((res) => {
      const c = new (getClient())();
      const t = setTimeout(() => { try { c.destroy(); } catch {} res(false); }, 25000);
      c.on('ready', () => { clearTimeout(t); try { c.end(); } catch {} res(true); })
        .on('error', () => { clearTimeout(t); res(false); })
        .connect({ ...SSH_OPTS, host: jumpHost, password: pw, hostHash: 'sha256', hostVerifier: makeHostVerifier(jumpHost) });
    });
    if (probe) { outerPw = pw; break; }
  }
  if (!outerPw) { console.error('mt_mactelnet: no pool credential connected to jump host'); process.exit(1); }
  // Inner hop: same pool list (site-standard). Each inner attempt opens its
  // own jump shell; attempts are serial (slow pacing, dropbear-friendly).
  let lastTranscript = '';
  for (const pw of SECRETS) {
    const r = await shellSessionTwoPw(jumpHost, outerPw, pw, targetMac, cmds);
    lastTranscript = r.transcript.join('');
    if (r.ok) {
      console.log(redactSecrets(stripAnsi(lastTranscript).slice(-4000), SECRETS));
      console.error(`mt_mactelnet: complete${allowWrite ? ` (WRITE MODE, incident ${incidentId})` : ''}`);
      process.exit(0);
    }
    if (r.reason !== 'inner-auth-failed') {
      console.error(`mt_mactelnet: ${r.reason}`);
      console.error(redactSecrets(stripAnsi(lastTranscript).slice(-800), SECRETS));
      process.exit(1);
    }
  }
  console.error('mt_mactelnet: inner auth failed for all pool credentials (no fallback guessed)');
  process.exit(1);
}
function stripAnsi(s) {
  return String(s).replace(/\x1b\[[0-9;?]*[a-zA-Z]/g, '').replace(/\x1b[()][0-9A-Z]/g, '').replace(/\r/g, '');
}
function shellSessionTwoPw(jump, outerPw, innerPw, target, commands, overallMs = 120000) {
  const jumpHost = jump;
  // Same state machine as shellSession but the jump uses outerPw and the
  // mac-telnet Login:/Password: exchange uses innerPw.
  return new Promise((resolve) => {
    const conn = new (getClient())();
    let buf = '';
    let stage = 'outer';
    let cmdIdx = -1;
    const transcript = [];
    const kill = (ok, reason) => { try { conn.destroy(); } catch {} resolve({ ok, reason, transcript }); };
    const timer = setTimeout(() => kill(false, 'overall-timeout'), overallMs);
    const done = (ok, reason) => { clearTimeout(timer); kill(ok, reason); };
    conn.on('ready', () => {
      conn.shell({ term: 'vt100', rows: 40, cols: 120 }, (err, stream) => {
        if (err) return done(false, 'shell-fail: ' + err.message);
        const put = (s) => stream.write(s + '\r');
        const lastLine = () => (buf.split('\n').pop() || '');
        stream.on('data', (d) => {
          const t = d.toString();
          buf += t;
          transcript.push(t);
          const ll = lastLine();
          if (stage === 'outer' && buf.includes('] >') && />\s*$/.test(ll)) {
            buf = ''; stage = 'inner-login'; put(`/tool mac-telnet ${target}`);
          } else if (stage === 'inner-login' && /Login:\s*$/.test(ll)) {
            buf = ''; put('admin');
          } else if (stage === 'inner-login' && /Password:\s*$/.test(ll)) {
            buf = ''; stage = 'inner-auth'; put(innerPw);
          } else if (stage === 'inner-auth' && /\] >\s*$/.test(ll)) {
            buf = ''; stage = 'cmds'; cmdIdx = 0; put(commands[0]);
          } else if (stage === 'cmds' && /\] >\s*$/.test(ll)) {
            cmdIdx++;
            if (cmdIdx >= commands.length) { stage = 'inner-quit'; put('/quit'); }
            else { buf = ''; put(commands[cmdIdx]); }
          } else if (stage === 'inner-quit' && (/\] >\s*$/.test(ll) || /Connection closed/i.test(buf))) {
            return done(true, 'complete');
          } else if (/Login failed|incorrect password|denied/i.test(buf)) {
            return done(false, 'inner-auth-failed');
          }
        });
      });
    }).on('error', (e) => done(false, 'conn-error: ' + e.message)).connect({ ...SSH_OPTS, host: jumpHost, password: outerPw, hostHash: 'sha256', hostVerifier: makeHostVerifier(jumpHost) });
  });
}
