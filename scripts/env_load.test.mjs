import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { loadEnvPrefixes, parseDotenv } from './env_load.mjs';

test('only the requested prefixes are loaded, and existing values win', () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'envload-'));
  try {
    const file = path.join(dir, '.env');
    writeFileSync(file, 'TRACKER_TAB="Tickets"\nTRACKER_AUDIT_FILE=a.jsonl # comment\nNOC_MCP_PASSPHRASE=hunter2-passphrase\nSONAR_TOKEN=tok\nTRACKER_EMPTY=\n');
    const env = { TRACKER_TAB: 'already' };
    const set = loadEnvPrefixes(['TRACKER_'], { env, file });
    assert.deepEqual(set, ['TRACKER_AUDIT_FILE']);
    assert.equal(env.TRACKER_TAB, 'already');
    assert.equal(env.TRACKER_AUDIT_FILE, 'a.jsonl');
    assert.equal('NOC_MCP_PASSPHRASE' in env, false);
    assert.equal('SONAR_TOKEN' in env, false);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('a missing file is not an error and nothing is expanded', () => {
  assert.deepEqual(loadEnvPrefixes(['X_'], { env: {}, file: '/nonexistent/.env' }), []);
  assert.deepEqual(parseDotenv('A="$(echo hi)"\n'), [['A', '$(echo hi)']]);
});
