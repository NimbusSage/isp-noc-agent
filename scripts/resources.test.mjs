import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { getResource, resolveResource, normalizeResource, notRegistered } from './resources.mjs';

const SHEET = '1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789';
const HERE = path.dirname(fileURLToPath(import.meta.url));

function fixture(profile, files = {}) {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'res-'));
  const pack = path.join(dir, 'site-pack');
  mkdirSync(path.join(pack, 'topology'), { recursive: true });
  if (profile) writeFileSync(path.join(pack, 'profile.json'), JSON.stringify(profile));
  for (const [rel, text] of Object.entries(files)) writeFileSync(path.join(dir, rel), text);
  const env = { NOC_PROFILE_FILE: path.join(pack, 'profile.json'), NOC_SITE_PACK_DIR: pack, NOC_DOTENV: path.join(dir, '.env') };
  return { dir, env, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

test('unset resource throws the next action', () => {
  const f = fixture(null);
  try {
    assert.throws(() => getResource('uisp', { env: f.env }), /Ask the operator/);
    assert.equal(resolveResource('uisp', { env: f.env }).value, '');
    assert.match(notRegistered('uisp'), /do not take one from a skill example/);
  } finally { f.cleanup(); }
});

test('profile, legacy host, env and .env sources follow the documented order', () => {
  const f = fixture({ org: { uisp_host: 'uisp.corp-net.org' }, resources: { librenms: { url: 'https://lnms.corp-net.org/' } } },
    { '.env': 'MYMATE_URL="https://mm.corp-net.org"\n' });
  try {
    assert.equal(getResource('uisp', { env: f.env }), 'https://uisp.corp-net.org');
    assert.equal(getResource('librenms', { env: f.env }), 'https://lnms.corp-net.org');
    assert.equal(getResource('mymate', { env: f.env }), 'https://mm.corp-net.org');
    const r = resolveResource('librenms', { env: { ...f.env, LIBRENMS_URL: 'http://10.1.2.3' } });
    assert.equal(r.value, 'http://10.1.2.3');
    assert.equal(r.others.length, 1);
  } finally { f.cleanup(); }
});

test('normalizeResource rejects credentials, queries and non-http schemes', () => {
  for (const bad of ['ftp://h.corp-net.org', 'https://u:p@h.corp-net.org', 'https://h.corp-net.org/?x=1', '']) {  // secret-scan:allow(fixture: a URL with credentials must be refused)
    assert.throws(() => normalizeResource(bad, 'url'));
  }
  assert.equal(normalizeResource('HTTPS://Host.Corp-Net.org:443/api/', 'url'), 'https://host.corp-net.org/api');
  assert.equal(normalizeResource(`https://docs.google.com/spreadsheets/d/${SHEET}/edit`, 'sheet'), SHEET);
});

test('the Python and Node resolvers agree on every case', () => {
  const cases = [
    { profile: { resources: { uisp: { url: 'https://Uisp.Corp-Net.org:443/' } } } },
    { profile: { org: { uisp_host: 'uisp.corp-net.org' } } },
    { profile: { resources: { tracker: { id: `https://docs.google.com/spreadsheets/d/${SHEET}/edit#gid=0` } } } },
    { profile: { resources: { librenms: { url: 'http://10.1.2.3:8080' } } }, extra: { LIBRENMS_URL: 'http://10.9.9.9' } },  // secret-scan:allow(fictional private addresses used as test input)
    { profile: {}, files: { 'site-pack/topology/temp-bgp-config.json': JSON.stringify({ oxidized_base_url: 'http://10.4.4.4:8888/' }) } },  // secret-scan:allow(fictional private addresses used as test input)
    { profile: null, files: { '.env': 'MYMATE_URL=https://mm.corp-net.org/\n' } },
    { profile: { resources: { uisp: { url: 'ftp://nope' } } } },
  ];
  for (const c of cases) {
    const f = fixture(c.profile, c.files);
    try {
      for (const name of ['uisp', 'librenms', 'mymate', 'oxidized', 'tracker']) {
        const env = { ...f.env, ...(c.extra || {}) };
        const py = spawnSync('python3', ['-B', path.join(HERE, 'resources.py'), 'list', '--json'], {
          env: { PATH: process.env.PATH, ...env }, encoding: 'utf8',
        });
        assert.equal(py.status, 0, py.stderr);
        const row = JSON.parse(py.stdout).find((r) => r.name === name);
        const js = resolveResource(name, { env });
        assert.equal(js.value || null, row.value, `${name} value for ${JSON.stringify(c)}`);
        assert.equal(js.others.length > 0, row.conflict.length > 0, `${name} conflict for ${JSON.stringify(c)}`);
      }
    } finally { f.cleanup(); }
  }
});
