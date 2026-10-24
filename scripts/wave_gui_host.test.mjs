// The Wave radio tool must refuse hosts the MCP server's host policy refuses, before it touches any
// credential. Runs the real script; needs the compiled MCP server (skipped when it is not built).
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const script = path.join(here, 'wave_gui.mjs');
const built = existsSync(path.join(here, '..', 'mcp-server', 'dist', 'security', 'hosts.js'));

function run(url, extraEnv = {}) {
  const cwd = mkdtempSync(path.join(tmpdir(), 'wavegui-'));
  try {
    return spawnSync(process.execPath, [script, 'read', '--url', url], {
      cwd, encoding: 'utf8', timeout: 30000,
      env: { PATH: process.env.PATH, HOME: cwd, NOC_DOTENV: path.join(cwd, 'absent.env'), ...extraEnv },
    });
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
}

test('a public address is refused before any credential is read', { skip: !built }, () => {
  const r = run('https://203.0.113.9');
  assert.equal(r.status, 2);
  assert.match(r.stderr, /refusing 203\.0\.113\.9/);
  assert.doesNotMatch(r.stderr, /credstore/);
});

test('a hostname is refused unless it is explicitly allowed', { skip: !built }, () => {
  const r = run('https://radio.example.invalid');
  assert.equal(r.status, 2);
  assert.match(r.stderr, /refusing radio\.example\.invalid/);
});

test('an address the operator allowed passes the policy and then stops at the missing credentials', { skip: !built }, () => {
  const r = run('https://203.0.113.9', { NOC_READ_ALLOWED_HOSTS: '203.0.113.0/24' });
  assert.equal(r.status, 2);
  assert.doesNotMatch(r.stderr, /refusing/);
  assert.match(r.stderr, /credstore|WAVE_GUI_USER/);
});

test('private space is allowed by default', { skip: !built }, () => {
  const r = run('https://10.0.255.1');
  assert.equal(r.status, 2);
  assert.doesNotMatch(r.stderr, /refusing/);
});

test('without the compiled MCP server the tool refuses instead of skipping the check', () => {
  if (built) return;   // covered by the other tests when it is built
  const r = run('https://10.0.255.1');
  assert.equal(r.status, 2);
  assert.match(r.stderr, /host policy unavailable/);
});
