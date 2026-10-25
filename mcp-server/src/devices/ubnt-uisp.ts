import type { DeviceTypeCredential } from '../credentials/schema.js';
import type { DeviceCommand, CommandResult, DeviceClient } from './types.js';
import { renderTemplate } from '../commands/params.js';

const DEFAULT_COMMAND_TIMEOUT_MS = 30000;

export class UBNTUispClient implements DeviceClient {
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
    // UISP uses stateless HTTP — no persistent connection needed
    // Verify connectivity with a simple request
    // (fetch needs an absolute URL: a relative path throws 'Failed to parse URL')
    try {
      await this.request(this.buildUrl('/nms/api/v2.1/nms/info'));
    } catch (err) {
      throw new Error(`UISP connection failed: ${err instanceof Error ? err.message : 'unknown'}`);
    }
  }

  async execute(command: DeviceCommand, params: Record<string, unknown>): Promise<CommandResult> {
    const startTime = Date.now();

    try {
      // Rendered inside the try so an invalid parameter becomes an 'error'
      // result instead of an exception. Values are validated against the
      // param schema AND percent-encoded (no ../ or ?# can survive).
      const url = this.buildUrl(renderTemplate(command, params, encodeURIComponent));
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
    const protocol = this.port === 443 ? 'https' : 'http';
    return `${protocol}://${this.host}:${this.port}${path}`;
  }

  private async request(url: string): Promise<unknown> {
    const token = this.credential.apiToken ?? this.credential.password;
    const response = await fetch(url, {
      headers: {
        'x-auth-token': token,
        'Content-Type': 'application/json',
      },
      signal: AbortSignal.timeout(this.timeoutMs),
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    return response.json();
  }
}