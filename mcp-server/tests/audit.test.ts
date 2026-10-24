import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mkdirSync, rmSync, readFileSync, existsSync, writeFileSync } from 'fs';
import { join } from 'path';

const TEST_LOG_DIR = '/tmp/noc-mcp-test-audit';

// Mock fs module at top level so it applies to all imports
const realFs = await import('fs');
vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    renameSync: vi.fn(actual.renameSync),
  };
});

// Re-import after mock setup
const { AuditLogger } = await import('../src/audit/logger.js');
const fsMock = await import('fs');

describe('AuditLogger', () => {
  let logger: InstanceType<typeof AuditLogger>;

  beforeEach(() => {
    vi.clearAllMocks();
    mkdirSync(TEST_LOG_DIR, { recursive: true });
    logger = new AuditLogger({
      path: join(TEST_LOG_DIR, 'audit.log'),
      maxFileSizeMb: 1,
      maxFiles: 3,
    });
  });

  afterEach(() => {
    rmSync(TEST_LOG_DIR, { recursive: true, force: true });
  });

  it('logs events without secrets', () => {
    logger.log({
      timestamp: new Date().toISOString(),
      device: 'tower-01',
      command: 'system-resource',
      category: 'system',
      outcome: 'success',
      redactions: 0,
      durationMs: 150,
    });

    const logFile = join(TEST_LOG_DIR, 'audit.log');
    expect(existsSync(logFile)).toBe(true);
    const content = readFileSync(logFile, 'utf-8');
    expect(content).toContain('tower-01');
    expect(content).toContain('system-resource');
    expect(content).not.toContain('password');
    expect(content).not.toContain('secret');
  });

  it('never logs credential material', () => {
    logger.log({
      timestamp: new Date().toISOString(),
      device: 'tower-01',
      command: 'login',
      category: 'auth',
      outcome: 'failed',
      redactions: 3,
      durationMs: 5000,
      metadata: { username: 'admin', password: 'hunter2' },
    });

    const logFile = join(TEST_LOG_DIR, 'audit.log');
    const content = readFileSync(logFile, 'utf-8');
    expect(content).not.toContain('hunter2');
  });
});

describe('Audit rotation failure logging', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    rmSync(TEST_LOG_DIR, { recursive: true, force: true });
  });

  it('logs rotation failures to stderr', () => {
    mkdirSync(TEST_LOG_DIR, { recursive: true });
    const logPath = join(TEST_LOG_DIR, 'audit.log');

    const logger = new AuditLogger({
      path: logPath,
      maxFileSizeMb: 0.001,
      maxFiles: 3,
    });

    // Write enough data to exceed the tiny threshold
    writeFileSync(logPath, 'x'.repeat(2048));

    // Make renameSync throw EACCES for audit.log renames
    const originalRenameSync = realFs.renameSync;
    (fsMock.renameSync as ReturnType<typeof vi.fn>).mockImplementation(
      (from: string | URL, to: string | URL) => {
        if (String(from).includes('audit.log')) {
          throw Object.assign(new Error('Permission denied'), { code: 'EACCES' });
        }
        return originalRenameSync(from, to);
      },
    );

    const stderrSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    logger.log({
      timestamp: new Date().toISOString(),
      device: 'tower-01',
      command: 'system-resource',
      category: 'system',
      outcome: 'success',
      redactions: 0,
      durationMs: 100,
    });

    expect(stderrSpy).toHaveBeenCalled();
    const callArgs = stderrSpy.mock.calls[0][0];
    expect(callArgs).toContain('[audit] rotation failure');

    stderrSpy.mockRestore();
  });
});
