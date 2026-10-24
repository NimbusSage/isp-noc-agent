#!/usr/bin/env node
// Shared Google user-OAuth for NOC skills ("agent acts as the operator").
//
// One Desktop OAuth client + one owner-only secret file outside the repo:
//   { "client_id": "...", "client_secret": "...", "refresh_token": "..." }
// Any skill imports getGoogleUserToken(scope) and acts with the consenting
// Google user's own access. New scopes = one more oauth-url consent from the
// operator; the stored refresh token transparently covers all granted scopes.
//
// Threat boundary: the secret file must live outside the repo with mode 0600
// (parent 0700). It is readable by any same-UID/root process on this host,
// including the webhook agent — same boundary as today's service-account keys.
// Prefer a dedicated Workspace user (not a personal account) for the grant.
import { chmodSync, closeSync, constants, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, realpathSync, renameSync, statSync, unlinkSync, writeSync } from 'node:fs';
import { createServer as createHttpServer } from 'node:http';
import { randomBytes } from 'node:crypto';
import { basename, dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token';
export const GOOGLE_SHEETS_READONLY = 'https://www.googleapis.com/auth/spreadsheets.readonly';
export const GOOGLE_SHEETS = 'https://www.googleapis.com/auth/spreadsheets';
export const GOOGLE_DRIVE_READONLY = 'https://www.googleapis.com/auth/drive.readonly';
export const GOOGLE_CALENDAR_EVENTS = 'https://www.googleapis.com/auth/calendar.events';
export const GOOGLE_CALENDAR_READONLY = 'https://www.googleapis.com/auth/calendar.readonly';
// Calendar events+readonly together cover: calendarList discovery (shared tech
// calendars visible to the consenting user), event insert on any writable
// calendar, and read-back verification — without full-calendar management ACLs.
export const GOOGLE_CALENDAR = `${GOOGLE_CALENDAR_EVENTS} ${GOOGLE_CALENDAR_READONLY}`;

const ALLOWED_SCOPES = new Set([
  GOOGLE_SHEETS_READONLY,
  GOOGLE_SHEETS,
  GOOGLE_DRIVE_READONLY,
  'https://www.googleapis.com/auth/drive.file',
  'openid',
  'https://www.googleapis.com/auth/userinfo.email',
  GOOGLE_CALENDAR_EVENTS,
  GOOGLE_CALENDAR_READONLY,
]);

const tokenCache = new Map();

export function assertSecretFileOutsideRepo(file, label) {
  const resolvedFile = realpathSync(file);
  const resolvedRoot = realpathSync(ROOT);
  const rel = relative(resolvedRoot, resolvedFile);
  if (rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`))) {
    throw new Error(`${label} must be outside the repository; .env should contain its path only`);
  }
  const stats = statSync(resolvedFile);
  if (!stats.isFile()) throw new Error(`${label} must be a regular file`);
  if (process.platform !== 'win32' && (stats.mode & 0o077) !== 0) {
    throw new Error(`${label} permissions are too broad; use an owner-only file (chmod 600)`);
  }
  return resolvedFile;
}

export function readOAuthSecrets(file, { requireRefresh = true } = {}) {
  const resolvedFile = assertSecretFileOutsideRepo(file, 'OAuth secret JSON');
  const secrets = JSON.parse(readFileSync(resolvedFile, 'utf8'));
  const clientId = secrets.client_id || secrets.clientId;
  const clientSecret = secrets.client_secret || secrets.clientSecret;
  const refreshToken = secrets.refresh_token || secrets.refreshToken;
  if (!clientId || !clientSecret) {
    throw new Error('OAuth file must contain client_id and client_secret from a Desktop OAuth client');
  }
  if (requireRefresh && !refreshToken) {
    throw new Error('OAuth file is missing refresh_token; complete the one-time code exchange first');
  }
  return { clientId, clientSecret, refreshToken };
}

/**
 * Write a secret file so it is NEVER readable by anyone else, not even briefly:
 * the content goes to a fresh sibling created with O_EXCL (`wx`) at mode 0600,
 * is fsync'd, and only then renamed over the target (atomic; a crash cannot leave
 * a half-written refresh token). Refuses a symlink / non-regular target and a
 * pre-existing target whose mode is wider than owner-only (that file may already
 * have been read by others, so the operator must look at it first).
 */
export function writeSecretFile(file, text) {
  const abs = resolve(file);
  const dir = dirname(abs);
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const target = join(realpathSync(dir), basename(abs));
  const rel = relative(realpathSync(ROOT), target);
  if (rel === '' || (rel !== '..' && !rel.startsWith(`..${sep}`))) {
    throw new Error('secret files must live outside the repository');
  }
  try {
    const st = lstatSync(target);
    if (st.isSymbolicLink() || !st.isFile()) throw new Error(`${target} exists and is not a regular file`);
    if (process.platform !== 'win32' && (st.mode & 0o077) !== 0) {
      throw new Error(`${target} already exists with permissions wider than owner-only; inspect it and chmod 600 (or remove it) first`);
    }
  } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }
  const tmp = join(dirname(target), `.${basename(target)}.${process.pid}.${randomBytes(6).toString('hex')}.tmp`);
  const fd = openSync(tmp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, 0o600);
  try {
    writeSync(fd, text);
    fsyncSync(fd);
  } catch (e) {
    closeSync(fd);
    try { unlinkSync(tmp); } catch { /* best effort */ }
    throw e;
  }
  closeSync(fd);
  chmodSync(tmp, 0o600);
  renameSync(tmp, target);
  return target;
}

export function storeRefreshToken(file, refreshToken) {
  const resolvedFile = assertSecretFileOutsideRepo(file, 'OAuth secret JSON');
  const stored = JSON.parse(readFileSync(resolvedFile, 'utf8'));
  stored.refresh_token = refreshToken;
  writeSecretFile(resolvedFile, JSON.stringify(stored, null, 2));
}

/**
 * The OAuth client secret for `init`. It is NEVER taken from argv (visible in
 * `ps` and shell history): use --client-secret-file <owner-only file outside the
 * repo> or the GOOGLE_CLIENT_SECRET environment variable.
 */
export function resolveClientSecret(args, env = process.env) {
  if (args['client-secret'] !== undefined) {
    throw new Error('--client-secret is no longer accepted: a secret on the command line shows up in `ps` and shell history. '
      + 'Put it in an owner-only file and pass --client-secret-file <path>, or export GOOGLE_CLIENT_SECRET.');
  }
  let secret = '';
  if (args['client-secret-file']) {
    const file = assertSecretFileOutsideRepo(args['client-secret-file'], 'client secret file');
    secret = readFileSync(file, 'utf8').trim();
  } else if (env.GOOGLE_CLIENT_SECRET) {
    secret = String(env.GOOGLE_CLIENT_SECRET).trim();
  }
  if (!secret || /[\r\n]/.test(secret)) {
    throw new Error('client secret missing: pass --client-secret-file <path> (single-line, mode 600, outside the repo) or set GOOGLE_CLIENT_SECRET');
  }
  return secret;
}

export function cliInit(args, env = process.env) {
  const file = args['oauth-file'];
  if (!file || !args['client-id']) {
    throw new Error('usage: init --oauth-file <path> --client-id <id> (--client-secret-file <path> | GOOGLE_CLIENT_SECRET=...)');
  }
  const clientSecret = resolveClientSecret(args, env);
  return writeSecretFile(file, JSON.stringify({ client_id: args['client-id'], client_secret: clientSecret }, null, 2) + '\n');
}

export function assertScopeAllowed(scope) {
  for (const part of String(scope).split(/\s+/)) {
    if (!ALLOWED_SCOPES.has(part)) {
      throw new Error(`Google scope not allowlisted for NOC use: ${part}`);
    }
  }
}

export function buildUserConsentUrl({ clientId, redirectUri = 'http://127.0.0.1:53682/callback' }, scope = GOOGLE_SHEETS) {
  assertScopeAllowed(scope);
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: 'code',
    scope,
    access_type: 'offline',
    prompt: 'consent',
  });
  return `https://accounts.google.com/o/oauth2/v2/auth?${params.toString()}`;
}

export async function exchangeAuthCode(secrets, code, redirectUri, fetchImpl = fetch) {
  const response = await fetchImpl(GOOGLE_TOKEN_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: secrets.clientId,
      client_secret: secrets.clientSecret,
      code,
      redirect_uri: redirectUri,
    }),
    signal: AbortSignal.timeout(20_000),
  });
  if (!response.ok) throw new Error(`Google OAuth code exchange failed (HTTP ${response.status}); code may be expired or redirect mismatch`);
  const body = await response.json();
  if (typeof body.refresh_token !== 'string' || !body.refresh_token) {
    throw new Error('Code exchange did not return a refresh_token; ensure access_type=offline and prompt=consent');
  }
  return body;
}

/** Skill entry point: short-lived access token for any allowlisted scope. */
export async function getGoogleUserToken(oauthFile, scope, fetchImpl = fetch) {
  assertScopeAllowed(scope);
  const secrets = readOAuthSecrets(oauthFile);
  const cacheKey = `user:${secrets.clientId}:${scope}`;
  const cached = tokenCache.get(cacheKey);
  if (cached && cached.expiresAt > Date.now() + 60_000) return cached.token;
  const response = await fetchImpl(GOOGLE_TOKEN_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'refresh_token',
      client_id: secrets.clientId,
      client_secret: secrets.clientSecret,
      refresh_token: secrets.refreshToken,
    }),
    signal: AbortSignal.timeout(20_000),
  });
  if (!response.ok) throw new Error(`Google OAuth refresh failed (HTTP ${response.status}); re-consent or check client secret`);
  const body = await response.json();
  const expiresIn = Number(body.expires_in);
  if (typeof body.access_token !== 'string' || !body.access_token || !Number.isFinite(expiresIn) || expiresIn <= 0) {
    throw new Error('Google OAuth refresh response omitted a valid access token/expiry');
  }
  tokenCache.set(cacheKey, { token: body.access_token, expiresAt: Date.now() + expiresIn * 1000 });
  return body.access_token;
}

// =====================================================================
// CLI: mint and verify OAuth secret files without hand-editing JSON.
//
//   GOOGLE_CLIENT_SECRET=<SECRET> node scripts/google_user_auth.mjs init \
//        --oauth-file ~/.config/nocagent/tracker_oauth.json --client-id <APP_ID>
//   (or --client-secret-file <owner-only file>; the old --client-secret <SECRET>
//    argv form is rejected because argv is world-readable via `ps`)
//   node scripts/google_user_auth.mjs authorize \
//        --oauth-file <path> --scopes "https://www.googleapis.com/auth/spreadsheets.readonly"
//   node scripts/google_user_auth.mjs refresh \
//        --oauth-file <path> --scope openid        # doctor check (silent ok/fail)
//   node scripts/google_user_auth.mjs status --oauth-file <path>
//
// `authorize` starts a one-shot loopback server on 127.0.0.1:53682, prints
// the consent URL, waits for Google's redirect, exchanges the code, and
// stores the refresh token into the same 0600 file. No tokens printed.
// =====================================================================

export function parseCliArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      const key = argv[i].slice(2);
      const next = argv[i + 1];
      args[key] = next && !next.startsWith('--') ? (argv.splice(i + 1, 1)[0] ?? 'true') : 'true';
    }
  }
  return args;
}

async function cliAuthorize(file, scope) {
  if (!scope) throw new Error('authorize requires --scopes "<space-separated scopes>"');
  const secrets = readOAuthSecrets(file, { requireRefresh: false });
  const redirectUri = 'http://127.0.0.1:53682/callback';
  const url = buildUserConsentUrl({ clientId: secrets.clientId, redirectUri }, scope);
  console.log('\nOpen this URL, consent with the OPERATOR Google account, and it will bounce back here:\n');
  console.log(url);
  console.log('\nWaiting on http://127.0.0.1:53682/callback ...\n');

  const code = await new Promise((resolvePromise, rejectPromise) => {
    const server = createHttpServer((req, res) => {
      try {
        const parsed = new URL(req.url, redirectUri);
        if (parsed.pathname !== '/callback') { res.writeHead(404); res.end('not found'); return; }
        const err = parsed.searchParams.get('error');
        const authCode = parsed.searchParams.get('code');
        res.writeHead(200, { 'content-type': 'text/html' });
        res.end(`<html><body style="font-family:sans-serif"><h3>${err ? 'Authorization failed: ' + err : 'Authorized — you can close this tab.'}</h3><p>Return to the terminal.</p></body></html>`);
        if (err) { server.close(); rejectPromise(new Error(`Consent failed: ${err}`)); return; }
        if (authCode) { server.close(); resolvePromise(authCode); }
      } catch (e) { res.writeHead(500); res.end('callback error'); rejectPromise(e); }
    });
    server.on('error', rejectPromise);
    server.listen(53682, '127.0.0.1');
    setTimeout(() => { server.close(); rejectPromise(new Error('Timed out waiting for OAuth callback (5 min)')); }, 5 * 60_000).unref();
  });

  const exchanged = await exchangeAuthCode(secrets, code, redirectUri);
  storeRefreshToken(file, exchanged.refresh_token);
  console.log('Refresh token stored. Re-run: node scripts/google_user_auth.mjs refresh --oauth-file <path> --scope openid');
}

async function cliRefresh(file, scope) {
  const token = await getGoogleUserToken(file, scope || 'openid');
  if (!token) throw new Error('No token returned');
  console.log(`OK — refresh token works${scope ? ` for scope: ${scope}` : ''}.`);
}

async function cliMain() {
  const args = parseCliArgs(process.argv);
  const cmd = process.argv[2];
  const file = args['oauth-file'];
  switch (cmd) {
    case 'init': {
      const written = cliInit(args);
      console.log(`Scaffolded ${written} (mode 600). Next: authorize --oauth-file <path> --scopes "..."`);
      break;
    }
    case 'authorize': await cliAuthorize(file, args.scopes); break;
    case 'refresh': await cliRefresh(file, args.scope); break;
    case 'status': {
      const s = readOAuthSecrets(file, { requireRefresh: false });
      console.log(JSON.stringify({ file, has_client_id: !!s.clientId, has_client_secret: !!s.clientSecret, has_refresh_token: !!s.refreshToken }, null, 2));
      break;
    }
    default:
      console.log('commands: init | authorize | refresh | status  (see header comment in this file)');
      process.exitCode = cmd ? 2 : 0;
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  cliMain().catch((e) => { console.error(`google_user_auth: ${e.message}`); process.exit(1); });
}
