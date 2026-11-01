import type { DeviceTypeCredential } from '../credentials/schema.js';
import type { DeviceCommand, CommandResult, DeviceClient } from './types.js';
import { request as httpsRequest, type RequestOptions as HttpsRequestOptions } from 'node:https';
import { request as httpRequest, type RequestOptions as HttpRequestOptions } from 'node:http';
import type { TLSSocket } from 'node:tls';
import { observeTlsFingerprint } from '../security/hostkeys.js';

const DEFAULT_COMMAND_TIMEOUT_MS = 30000;
const u0__debug = (u: string): string => { try { const x = new URL(u); return x.host; } catch { return 'invalid'; } };

interface RawResponse { status: number; location?: string; cookies: string[]; body: string; }

export class UBNTAirosClient implements DeviceClient {
  private host: string;
  private port: number;
  private credential: DeviceTypeCredential;
  private timeoutMs: number;
  private sessionCookie: string | null = null;

  constructor(host: string, port: number, credential: DeviceTypeCredential, timeoutMs: number = DEFAULT_COMMAND_TIMEOUT_MS) {
    this.host = host;
    this.port = port;
    this.credential = credential;
    this.timeoutMs = timeoutMs;
  }

  async connect(): Promise<void> {
    // FIX (was: this.request('/status.cgi')) — request() does new URL(...),
    // and a bare path throws "Invalid URL" before any credential is tried,
    // which every exact-device attempt then reported as an auth failure.
    try {
      await this.request(this.buildUrl('/status.cgi'));
    } catch (err) {
      throw new Error(`airOS connection failed: ${err instanceof Error ? err.message : 'unknown'}`);
    }
  }

  async execute(command: DeviceCommand, params: Record<string, unknown>): Promise<CommandResult> {
    const startTime = Date.now();
    const url = this.buildUrl(command.template);

    try {
      const response = await this.request(url);
      return {
        device: this.host,
        command: command.name,
        category: command.category,
        status: 'success',
        result: response,
        warnings: [],
        redactions: 0,
        timestamp: new Date().toISOString(),
        durationMs: Date.now() - startTime,
      };
    } catch (error) {
      return {
        device: this.host,
        command: command.name,
        category: command.category,
        status: 'error',
        result: null,
        warnings: [error instanceof Error ? error.message : 'Unknown error'],
        redactions: 0,
        timestamp: new Date().toISOString(),
        durationMs: Date.now() - startTime,
      };
    }
  }

  async disconnect(): Promise<void> {
    // No persistent connection
  }

  buildUrl(path: string): string {
    return `http://${this.host}:${this.port}${path}`;
  }

  private authHeaders(): Record<string, string | undefined> {
    const auth = Buffer.from(`${this.credential.username}:${this.credential.password}`).toString('base64');
    const h: Record<string, string | undefined> = { 'Authorization': `Basic ${auth}` };
    if (this.sessionCookie) h.Cookie = this.sessionCookie;
    return h;
  }

  private async fetchOnce(current: string, method: string, body: string | null, headers: Record<string, string | undefined>): Promise<RawResponse> {
    const u = new URL(current);
    const secure = u.protocol === 'https:';
    const opts: HttpRequestOptions & HttpsRequestOptions = {
      hostname: u.hostname,
      port: u.port ? Number(u.port) : secure ? 443 : 80,
      path: u.pathname + u.search,
      method,
      headers,
      ...(secure ? { rejectUnauthorized: false } : {}),
    };
    return new Promise<RawResponse>((resolve, reject) => {
      const req = (secure ? httpsRequest : httpRequest)(opts, (r) => {
        const chunks: Buffer[] = [];
        r.on('data', (c: Buffer) => chunks.push(c));
        r.on('end', () => resolve({
          status: r.statusCode ?? 0,
          location: r.headers.location,
          cookies: r.headers['set-cookie'] ?? [],
          body: Buffer.concat(chunks).toString('utf8'),
        }));
        r.on('error', reject);
      });
      req.on('error', reject);
      if (secure) {
        // Observe-only certificate fingerprint recording/audit (see security/hostkeys.ts);
        // never blocks, because credentials already crossed HTTP:80 before the redirect.
        req.on('socket', (sock) => {
          sock.once('secureConnect', () => {
            try { observeTlsFingerprint(u.hostname, Number(opts.port), (sock as TLSSocket).getPeerCertificate().fingerprint256); } catch { /* observation only */ }
          });
        });
      }
      req.setTimeout(this.timeoutMs, () => req.destroy(new Error(`airOS request timed out after ${this.timeoutMs}ms`)));
      if (body) req.write(body);
      req.end();
    });
  }

  /**
   * airOS 8 (LBE/LiteAP/LiteBeam gen2 firmware) gates CGI endpoints behind a
   * session cookie, not Basic auth: a plain authenticated GET of /status.cgi
   * returns 403 even with CORRECT credentials (verified live 2026-11-01:
   * Zellman5n, airOS 8.5.12). The documented exchange is POST /login.cgi with
   * form-encoded username/password; the response sets AIROS_SESSIONID, which
   * all subsequent CGI requests must carry. One login per client, one retry.
   */
  private async sessionLogin(): Promise<void> {
    const body = JSON.stringify({ username: this.credential.username, password: this.credential.password });
    const loginHeaders = () => {
      // python-airos equivalence: the login POST carries no Basic header —
      // the session cookie established by this exchange is the sole auth.
      const h: Record<string, string | undefined> = {
        'Content-Type': 'application/json',
        'Content-Length': String(Buffer.byteLength(body)),
      };
      if (this.sessionCookie) h.Cookie = this.sessionCookie;
      return h;
    };
    // airOS 8 login (matching python-airos / Home Assistant): POST JSON
    // {username,password} to /api/auth; success emits an AIROS_* cookie. If
    // the device is airOS 6 (XM) the /api/auth endpoint does not exist, and
    // the legacy /login.cgi form-encoded exchange is used instead
    // (GET seeds a cookie, POST form with username/password/uri expects 302,
    // then GET /index.cgi activates the session).
    let res = await this.fetchOnce(`https://${this.host}/api/auth`, 'POST', body, loginHeaders());
    let cookieLine = res.cookies.find((c) => c.startsWith('AIROS_'));
    if (process.env.AIROS_DEBUG === '1') console.error(`[airos-debug] auth: status=${res.status} loc=${res.location ?? '-'} cookies=${res.cookies.map(c=>c.split('=')[0]).join(',') || 'none'} bodyLen=${res.body.length}`);
    // Follow the /api/auth redirect chain (POST → GET like a browser); the
    // AIROS_* cookie may be emitted by any response in the chain.
    for (let hop = 0; !cookieLine && [301, 302, 303, 307, 308].includes(res.status) && res.location && hop < 5; hop++) {
      const next = new URL(res.location, this.buildUrl('/api/auth')).toString();
      res = await this.fetchOnce(next, 'GET', null, this.authHeaders());
      if (process.env.AIROS_DEBUG === '1') console.error(`[airos-debug] auth-chain: get ${u0__debug(next)}: status=${res.status} loc=${res.location ?? '-'} cookies=${res.cookies.map(c=>c.split('=')[0]).join(',') || 'none'} bodyLen=${res.body.length}`);
      cookieLine = res.cookies.find((c) => c.startsWith('AIROS_'));
    }
    if (!cookieLine && res.status === 404) {
      // Legacy v6 flow
      let current = this.buildUrl('/login.cgi');
      res = await this.fetchOnce(current, 'GET', null, this.authHeaders());
      const seed = res.cookies.find((c) => c.startsWith('AIROS_WIN_SESSIONID=') || c.startsWith('AIROS_SESSIONID='));
      if (seed) this.sessionCookie = seed.split(';')[0];
      const formBody = `username=${encodeURIComponent(this.credential.username)}&password=${encodeURIComponent(this.credential.password)}&uri=%2Findex.cgi`;
      for (let hop = 0; hop < 4; hop++) {
        res = await this.fetchOnce(current, 'POST', formBody, loginHeaders());
        cookieLine = res.cookies.find((c) => c.startsWith('AIROS_'));
        if (cookieLine) break;
        if ([301, 302, 303, 307, 308].includes(res.status) && res.location) {
          current = new URL(res.location, current).toString();
          continue;
        }
        break;
      }
      if (cookieLine) {
        this.sessionCookie = cookieLine.split(';')[0];
        await this.fetchOnce(this.buildUrl('/index.cgi'), 'GET', null, this.authHeaders());
        return;
      }
      throw new Error('airOS v6 login failed: no AIROS_* cookie emitted');
    }
    if (!cookieLine) {
      throw new Error(`airOS login failed (HTTP ${res.status}, body: ${JSON.stringify(res.body.slice(0, 60))}): no AIROS_* cookie emitted`);
    }
    this.sessionCookie = cookieLine.split(';')[0];
  }

  private async request(url: string): Promise<unknown> {
    // Plant radios serve HTTP→HTTPS redirects with self-signed certs, which
    // global fetch rejects (verified live: every pool 401-masked as a
    // TLS error on one AP). Follow up to 3 redirects with node:http(s)
    // and rejectUnauthorized:false scoped to THIS device connection only —
    // never a process-global TLS bypass. No new dependencies.
    let current = url;
    let res = await this.fetchOnce(current, 'GET', null, this.authHeaders());
    let loginAttempted = false;
    for (let hop = 0; hop < 6; hop++) {
      if (process.env.AIROS_DEBUG === '1') console.error(`[airos-debug] get ${u0__debug(current)}: status=${res.status} loc=${res.location ?? '-'} cookies=${res.cookies.map(c=>c.split('=')[0]).join(',') || 'none'} bodyLen=${res.body.length}`);
      if ([301, 302, 303, 307, 308].includes(res.status) && res.location) {
        current = new URL(res.location, current).toString();
        res = await this.fetchOnce(current, 'GET', null, this.authHeaders());
        continue;
      }
      if (res.status === 403 && !loginAttempted) {
        loginAttempted = true;
        await this.sessionLogin();
        res = await this.fetchOnce(current, 'GET', null, this.authHeaders());
        continue;
      }
      if (res.status < 200 || res.status >= 300) {
        throw new Error(`HTTP ${res.status}: ${res.body.slice(0, 120) || '(empty)'}`);
      }
      try {
        return JSON.parse(res.body);
      } catch {
        throw new Error(`HTTP 200 but body is not JSON (first 100 chars): ${res.body.slice(0, 100)}`);
      }
    }
    throw new Error('Too many redirects (plant radio did not settle on http/https)');
  }
}
