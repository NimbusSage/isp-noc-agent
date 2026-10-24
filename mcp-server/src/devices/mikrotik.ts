import { Client } from 'ssh2';
import type { DeviceTypeCredential } from '../credentials/schema.js';
import type { DeviceCommand, CommandResult, DeviceClient } from './types.js';
import { renderTemplate } from '../commands/params.js';
import { sshHostKeyOptions } from '../security/hostkeys.js';

const DEFAULT_COMMAND_TIMEOUT_MS = 30000;

export class MikroTikClient implements DeviceClient {
  private host: string;
  private port: number;
  private credential: DeviceTypeCredential;
  private conn: Client | null = null;
  private timeoutMs: number;

  constructor(host: string, port: number, credential: DeviceTypeCredential, timeoutMs: number = DEFAULT_COMMAND_TIMEOUT_MS) {
    this.host = host;
    this.port = port;
    this.credential = credential;
    this.timeoutMs = timeoutMs;
  }

  async connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      this.conn = new Client();
      this.conn.on('ready', () => resolve());
      this.conn.on('error', (err) => reject(err));
      this.conn.connect({
        host: this.host,
        port: this.port,
        username: this.credential.username,
        password: this.credential.password,
        readyTimeout: 15000,
        // Host-key pinning (NOC_SSH_HOSTKEY_MODE; default warn = record + audit, never block).
        ...sshHostKeyOptions(this.host, this.port),
        // The PoE-cycle dwell is timed in Node, so the session idles for up to
        // 60s: keep it alive rather than let a middlebox drop it.
        keepaliveInterval: 10000,
        keepaliveCountMax: 3,
        algorithms: {
          kex: ['curve25519-sha256', 'curve25519-sha256@libssh.org', 'ecdh-sha2-nistp256', 'ecdh-sha2-nistp384', 'ecdh-sha2-nistp521'],
          serverHostKey: ['ssh-rsa', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp521', 'ssh-ed25519'],
        },
      });
    });
  }

  async execute(command: DeviceCommand, params: Record<string, unknown>): Promise<CommandResult> {
    if (!this.conn) {
      await this.connect();
    }

    const startTime = Date.now();

    try {
      const cmd = this.buildCommand(command, params);
      const output = await this.execWithTimeout(cmd, this.timeoutMs);
      return {
        device: this.host,
        command: command.name,
        category: command.category,
        status: 'success',
        result: this.parseOutput(output, command),
        warnings: [],
        redactions: 0,
        timestamp: new Date().toISOString(),
        durationMs: Date.now() - startTime,
      };
    } catch (error) {
      const isTimeout = error instanceof Error && error.message.includes('timed out');
      return {
        device: this.host,
        command: command.name,
        category: command.category,
        status: isTimeout ? 'timeout' : 'error',
        result: null,
        warnings: [error instanceof Error ? error.message : 'Unknown error'],
        redactions: 0,
        timestamp: new Date().toISOString(),
        durationMs: Date.now() - startTime,
      };
    }
  }

  async disconnect(): Promise<void> {
    if (this.conn) {
      try {
        this.conn.end();
      } catch {
        // Ignore errors during disconnect
      }
      this.conn = null;
    }
  }

  buildCommand(command: DeviceCommand, params: Record<string, unknown>): string {
    // Validates every value against the command's param schema (throws), so a
    // caller that bypasses CommandValidator still cannot inject into the line.
    return renderTemplate(command, params);
  }

  /** Raw command execution for the gated temp-site executor only. The caller
   * (temp-ospf-apply/rollback) owns validation, gates, audit, and rollback;
   * this method never runs without those. Not exposed to generic diagnostics. */
  async runRaw(cmd: string): Promise<string> {
    if (!this.conn) {
      await this.connect();
    }
    return this.execWithTimeout(cmd, this.timeoutMs);
  }

  private async execCommand(cmd: string): Promise<string> {
    return new Promise((resolve, reject) => {
      if (!this.conn) {
        reject(new Error('Not connected'));
        return;
      }

      this.conn.exec(cmd, (err, stream) => {
        if (err) {
          reject(err);
          return;
        }

        let output = '';
        stream.on('data', (data: Buffer) => {
          output += data.toString();
        });
        stream.on('close', () => {
          resolve(output);
        });
        stream.on('error', (err: Error) => {
          reject(err);
        });
      });
    });
  }

  private async execWithTimeout(cmd: string, timeoutMs: number): Promise<string> {
    let timeoutId: NodeJS.Timeout;

    const timeoutPromise = new Promise<never>((_, reject) => {
      timeoutId = setTimeout(() => {
        reject(new Error(`Command timed out after ${timeoutMs}ms`));
      }, timeoutMs);
    });

    try {
      const result = await Promise.race([
        this.execCommand(cmd),
        timeoutPromise,
      ]);
      return result;
    } finally {
      clearTimeout(timeoutId!);
    }
  }

  private parseOutput(raw: string, command: DeviceCommand): unknown {
    // MikroTik as-value format: key=value pairs
    const lines = raw.trim().split('\n');
    const records: Record<string, string>[] = [];
    let current: Record<string, string> = {};

    for (const line of lines) {
      if (line.startsWith('=')) {
        // as-value format: =key=value
        const match = line.match(/^=(\w+)=(.*)$/);
        if (match) {
          current[match[1]] = match[2];
        }
      } else if (line.includes('=')) {
        // Plain format: key=value
        const parts = line.split('=');
        if (parts.length >= 2) {
          current[parts[0].trim()] = parts.slice(1).join('=').trim();
        }
      } else if (line.trim() && Object.keys(current).length > 0) {
        // New record boundary
        records.push(current);
        current = {};
      }
    }

    if (Object.keys(current).length > 0) {
      records.push(current);
    }

    // If no records parsed, return raw text
    return records.length > 0 ? records : raw.trim();
  }
}
