import { appendFileSync, statSync, renameSync, mkdirSync } from 'fs';
import { dirname } from 'path';

export interface AuditEvent {
  timestamp: string;
  device: string;
  command: string;
  category: string;
  outcome: 'success' | 'error' | 'timeout' | 'auth_failed' | 'transport_error' | 'blocked' | 'warning';
  redactions: number;
  durationMs: number;
  metadata?: Record<string, unknown>;
}

interface AuditLoggerConfig {
  path: string;
  maxFileSizeMb: number;
  maxFiles: number;
}

const SENSITIVE_KEYS = new Set([
  'password', 'secret', 'token', 'apikey', 'api_key', 'auth',
  'credential', 'passphrase', 'private_key', 'snmp_community',
]);

export class AuditLogger {
  private config: AuditLoggerConfig;

  constructor(config: AuditLoggerConfig) {
    this.config = config;
    mkdirSync(dirname(config.path), { recursive: true });
  }

  log(event: AuditEvent): void {
    const sanitized = this.sanitize(event);
    const line = JSON.stringify(sanitized) + '\n';

    this.rotateIfNeeded();
    appendFileSync(this.config.path, line, 'utf-8');
  }

  private sanitize(event: AuditEvent): AuditEvent {
    if (!event.metadata) return event;

    const clean: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(event.metadata)) {
      if (SENSITIVE_KEYS.has(key.toLowerCase())) {
        clean[key] = '[REDACTED]';
      } else if (typeof value === 'string' && value.length > 200) {
        clean[key] = value.substring(0, 200) + '...[truncated]';
      } else {
        clean[key] = value;
      }
    }
    return { ...event, metadata: clean };
  }

  private rotateIfNeeded(): void {
    try {
      const stat = statSync(this.config.path);
      const sizeMb = stat.size / (1024 * 1024);
      if (sizeMb >= this.config.maxFileSizeMb) {
        for (let i = this.config.maxFiles - 1; i >= 1; i--) {
          const from = `${this.config.path}.${i}`;
          const to = `${this.config.path}.${i + 1}`;
          try { renameSync(from, to); } catch { /* ignore */ }
        }
        renameSync(this.config.path, `${this.config.path}.1`);
      }
    } catch (err) {
      if (err instanceof Error && 'code' in err && (err as NodeJS.ErrnoException).code === 'ENOENT') {
        // File doesn't exist yet — expected
        return;
      }
      console.error(`[audit] rotation failure: ${err instanceof Error ? err.message : err}`);
    }
  }
}
