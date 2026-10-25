import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, symlinkSync, writeFileSync, readFileSync, realpathSync, statSync, rmSync, existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { safeOutPath, defaultOutPath, resolveOxidizedBase, writeSecretFile, allowedOutRoots } from './oxidized_fetch.mjs';

function sandbox() {
  const base = mkdtempSync(path.join(os.tmpdir(), 'oxfetch-'));
  const root = path.join(realpathSync(base), 'tmproot');
  const outside = path.join(base, 'elsewhere');
  mkdirSync(root);
  mkdirSync(outside);
  return { base, root, outside, cleanup: () => rmSync(base, { recursive: true, force: true }) };
}

test('safeOutPath accepts files directly under an allowed root', () => {
  const s = sandbox();
  try {
    const realRoot = realpathSync(s.root);
    const got = safeOutPath(path.join(s.root, 'a.cfg'), [realRoot]);
    assert.equal(got, path.join(realRoot, 'a.cfg'));
  } finally { s.cleanup(); }
});

test('safeOutPath refuses traversal, siblings with a shared prefix, and the root itself', () => {
  const s = sandbox();
  try {
    assert.throws(() => safeOutPath(path.join(s.root, '..', 'elsewhere', 'a.cfg'), [s.root]), /must be under/);
    // /tmpevil-style sibling: the raw string starts with the root text but is not inside it
    const evil = `${s.root}evil`;
    mkdirSync(evil);
    assert.throws(() => safeOutPath(path.join(evil, 'a.cfg'), [s.root]), /must be under/);
    assert.throws(() => safeOutPath(s.root, [s.root]), /must be under/);
    assert.throws(() => safeOutPath('', [s.root]), /non-empty/);
    assert.throws(() => safeOutPath(`${s.root}${path.sep}..`, [s.root]), /must (name a file|be under)/);
  } finally { s.cleanup(); }
});

test('safeOutPath refuses a symlinked directory that escapes the root', () => {
  const s = sandbox();
  try {
    symlinkSync(s.outside, path.join(s.root, 'link'));
    assert.throws(() => safeOutPath(path.join(s.root, 'link', 'a.cfg'), [s.root]), /must be under/);
  } finally { s.cleanup(); }
});

test('safeOutPath refuses a symlink at the target and writeSecretFile will not follow one', () => {
  const s = sandbox();
  try {
    const victim = path.join(s.outside, 'victim.txt');
    writeFileSync(victim, 'keep me');
    const link = path.join(s.root, 'a.cfg');
    symlinkSync(victim, link);
    assert.throws(() => safeOutPath(link, [s.root]), /symlink/);
    assert.throws(() => writeSecretFile(link, 'secret'));          // O_NOFOLLOW
    assert.equal(readFileSync(victim, 'utf8'), 'keep me');
  } finally { s.cleanup(); }
});

test('safeOutPath requires the directory to exist', () => {
  const s = sandbox();
  try {
    assert.throws(() => safeOutPath(path.join(s.root, 'nope', 'a.cfg'), [s.root]), /does not exist/);
  } finally { s.cleanup(); }
});

test('writeSecretFile creates 0600 and tightens a pre-existing wide file', () => {
  const s = sandbox();
  try {
    const f = path.join(s.root, 'x.cfg');
    writeSecretFile(f, 'one');
    assert.equal(statSync(f).mode & 0o777, 0o600);
    writeFileSync(f, 'old', { mode: 0o644 });
    writeSecretFile(f, 'two');
    assert.equal(readFileSync(f, 'utf8'), 'two');
    assert.equal(statSync(f).mode & 0o777, 0o600);
  } finally { s.cleanup(); }
});

test('the default output path is accepted under the real /tmp', () => {
  if (!existsSync('/tmp')) return;
  const p = defaultOutPath('10.1.2.3');
  assert.equal(p, '/tmp/oxidized-10-1-2-3.cfg');
  const got = safeOutPath(p, allowedOutRoots());
  assert.ok(got.endsWith('oxidized-10-1-2-3.cfg'));
});

test('resolveOxidizedBase: env first, older config fallback, tolerant of missing files, validates scheme', () => {
  const none = () => { throw new Error('ENOENT'); };
  const env = (extra = {}) => ({ NOC_DOTENV: '/nonexistent/.env', NOC_PROFILE_FILE: '/nonexistent/profile.json', ...extra });
  assert.equal(resolveOxidizedBase(env({ OXIDIZED_BASE_URL: 'http://ox.corp-net.org:8888/' }), none), 'http://ox.corp-net.org:8888');
  const config = (file) => { if (file.endsWith('temp-bgp-config.json')) return JSON.stringify({ oxidized_base_url: 'https://cfg.corp-net.org/' }); throw new Error('ENOENT'); };
  assert.equal(resolveOxidizedBase(env(), config), 'https://cfg.corp-net.org');
  assert.equal(resolveOxidizedBase(env(), none), '');
  assert.throws(() => resolveOxidizedBase(env({ OXIDIZED_BASE_URL: 'file:///etc/passwd' }), none), /http\(s\)/);
});

