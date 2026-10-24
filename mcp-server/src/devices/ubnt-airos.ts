import type { DeviceTypeCredential } from '../credentials/schema.js';
import type { DeviceCommand, CommandResult, DeviceClient } from './types.js';
import { get as httpsGet, type RequestOptions as HttpsRequestOptions } from 'node:https';
import { get as httpGet, type RequestOptions as HttpRequestOptions } from 'node:http';
import type { TLSSocket } from 'node:tls';
import { observeTlsFingerprint } from '../security/hostkeys.js';

const DEFAULT_COMMAND_TIMEOUT_MS = 30000;

export class UBNTAirosClient implements DeviceClient {
  private host: string;
  private port: number;
  private credential: DeviceTypeCredential;
  private timeoutMs: number;

  constructor(host: string, port: number, credential: DeviceTypeCredential, timeoutMs: number = DEFAULT_COMMAND_TIMEOUT_MS) {
    this.host = host;
    this.port = port;
    this.credential = credential;
    this.timeoutMs = timeoutMs;
  }

  async connect(): Promise<void> {
    // airOS uses stateless HTTP — verify connectivity
    try {
      await this.request('/status.cgi');
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

  private async request(url: string): Promise<unknown> {
    // Plant radios serve HTTP→HTTPS redirects with self-signed certs, which
    // global fetch rejects (verified live: every pool 401-masked as a
    // TLS error on one AP). Follow up to 3 redirects with node:http(s)
    // and rejectUnauthorized:false scoped to THIS device connection only —
    // never a process-global TLS bypass. No new dependencies.
    const auth = Buffer.from(`${this.credential.username}:${this.credential.password}`).toString('base64');
    let current = url;
    for (let hop = 0; hop < 4; hop++) {
      const u = new URL(current);
      const secure = u.protocol === 'https:';
      const opts: HttpRequestOptions & HttpsRequestOptions = {
        hostname: u.hostname,
        port: u.port ? Number(u.port) : secure ? 443 : 80,
        path: u.pathname + u.search,
        method: 'GET',
        headers: { 'Authorization': `Basic ${auth}` },
        ...(secure ? { rejectUnauthorized: false } : {}),
      };
      const res = await new Promise<{ status: number; location?: string; body: string }>((resolve, reject) => {
        const req = (secure ? httpsGet : httpGet)(opts, (r) => {
          const chunks: Buffer[] = [];
          r.on('data', (c: Buffer) => chunks.push(c));
          r.on('end', () => resolve({
            status: r.statusCode ?? 0,
            location: r.headers.location,
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
      });
      if ([301, 302, 303, 307, 308].includes(res.status) && res.location) {
        current = new URL(res.location, current).toString();
        continue;
      }
      if (res.status < 200 || res.status >= 300) {
        throw new Error(`HTTP ${res.status}: ${res.body.slice(0, 120) || '(empty)'}`);
      }
      return JSON.parse(res.body);
    }
    throw new Error('Too many redirects (plant radio did not settle on http/https)');
  }
}