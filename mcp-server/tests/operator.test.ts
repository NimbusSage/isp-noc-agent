import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync, readFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { resolveOperator } from '../src/audit/operator.js';
import { AuditLogger } from '../src/audit/logger.js';

describe('resolveOperator', () => {
  let dir: string;
  beforeEach(() => { dir = mkdtempSync(join(tmpdir(), 'noc-operator-')); });
  afterEach(() => { rmSync(dir, { recursive: true, force: true }); });

  it('is undefined on a single-person install', () => {
    expect(resolveOperator({ NOC_USER_CONFIG: join(dir, 'absent.json') })).toBeUndefined();
  });

  it('takes NOC_OPERATOR first, then the per-user file', () => {
    const file = join(dir, 'user.json');
    writeFileSync(file, JSON.stringify({ operator: 'vic' }));
    expect(resolveOperator({ NOC_USER_CONFIG: file })).toBe('vic');
    expect(resolveOperator({ NOC_USER_CONFIG: file, NOC_OPERATOR: 'ada' })).toBe('ada');
    expect(resolveOperator({ NOC_USER_CONFIG: file, NOC_OPERATOR: 'batch' })).toBe('batch');
  });

  it('ignores values that are not a plain handle', () => {
    for (const bad of ['Ada Lovelace', 'x'.repeat(60), '../etc', 'a b', '1abc']) {
      expect(resolveOperator({ NOC_OPERATOR: bad, NOC_USER_CONFIG: join(dir, 'absent.json') })).toBeUndefined();
    }
    const file = join(dir, 'broken.json');
    writeFileSync(file, '{not json');
    expect(resolveOperator({ NOC_USER_CONFIG: file })).toBeUndefined();
  });

  it('is stamped on audit records unless the event already names one', () => {
    const path = join(dir, 'audit.log');
    const logger = new AuditLogger({ path, maxFileSizeMb: 1, maxFiles: 2, operator: 'ada' });
    const base = { timestamp: '2026-10-29T00:00:00Z', device: 'tower-01', command: 'system-resource', category: 'system',
      outcome: 'success' as const, redactions: 0, durationMs: 1 };
    logger.log(base);
    logger.log({ ...base, operator: 'batch' });
    const [first, second] = readFileSync(path, 'utf-8').trim().split('\n').map((l) => JSON.parse(l));
    expect(first.operator).toBe('ada');
    expect(second.operator).toBe('batch');
  });
});
