import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import {
  parseDotEnv,
  loadDotEnvFile,
  ensureRepoEnv,
} from '../src/config/repo-env.js';

describe('repo .env fallback loader', () => {
  it('parses comments, export prefix, quotes, and = in values', () => {
    const parsed = parseDotEnv([
      '# comment',
      '',
      'PLAIN=hello',
      'export EXPORTED=world',
      'SINGLE=\'quoted value\'',
      'DOUBLE="quoted # not comment"',
      'EQUALS=a=b=c',
      'TRAILING=novalue # inline comment',
      'EMPTY=',
      '1BAD=nope',
      'NOEQUALS',
    ].join('\n'));
    expect(parsed.PLAIN).toBe('hello');
    expect(parsed.EXPORTED).toBe('world');
    expect(parsed.SINGLE).toBe('quoted value');
    expect(parsed.DOUBLE).toBe('quoted # not comment');
    expect(parsed.EQUALS).toBe('a=b=c');
    expect(parsed.TRAILING).toBe('novalue');
    expect(parsed.EMPTY).toBe('');
    expect(parsed['1BAD']).toBeUndefined();
    expect(parsed.NOEQUALS).toBeUndefined();
  });

  describe('loadDotEnvFile (with real temp files)', () => {
    let dir: string;
    const touched: string[] = [];
    const setForTest = (k: string, v: string | undefined) => {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    };

    beforeEach(() => { dir = mkdtempSync(join(tmpdir(), 'noc-dotenv-')); });
    afterEach(() => {
      for (const k of touched) delete process.env[k];
      touched.length = 0;
      delete process.env.NOC_MCP_DOTENV;
      rmSync(dir, { recursive: true, force: true });
    });

    it('fills only unset vars; explicit env always wins', () => {
      const f = join(dir, '.env');
      writeFileSync(f, 'NOC_MCP_TEST_A=fromfile\nNOC_MCP_TEST_B=fromfile\n');
      touched.push('NOC_MCP_TEST_A', 'NOC_MCP_TEST_B');
      setForTest('NOC_MCP_TEST_A', undefined);
      setForTest('NOC_MCP_TEST_B', 'explicit');
      const filled = loadDotEnvFile(f);
      expect(filled).toBe(1);
      expect(process.env.NOC_MCP_TEST_A).toBe('fromfile');
      expect(process.env.NOC_MCP_TEST_B).toBe('explicit');
    });

    it('treats an EMPTY-STRING env var as unset (OpenCode {env:X} yields "" when X is absent)', () => {
      const f = join(dir, '.env');
      writeFileSync(f, 'NOC_MCP_TEST_E=fromfile\nNOC_MCP_TEST_F=fromfile\nNOC_MCP_TEST_G=\n');
      touched.push('NOC_MCP_TEST_E', 'NOC_MCP_TEST_F', 'NOC_MCP_TEST_G');
      setForTest('NOC_MCP_TEST_E', '');
      setForTest('NOC_MCP_TEST_F', 'explicit');
      setForTest('NOC_MCP_TEST_G', '');
      process.env.NOC_MCP_DOTENV = f;
      const st = ensureRepoEnv();
      expect(st.path).toBe(f);
      expect(process.env.NOC_MCP_TEST_E).toBe('fromfile'); // '' -> filled
      expect(process.env.NOC_MCP_TEST_F).toBe('explicit'); // non-empty explicit env still wins
      expect(process.env.NOC_MCP_TEST_G).toBe(''); // file value is empty too: stays empty
      expect(st.filled).toBe(2); // E and G are "filled" (G with the file's empty value)
    });

    it('fills an empty NOC_MCP_PASSPHRASE from the repo .env but never overrides a real one', () => {
      const f = join(dir, '.env');
      writeFileSync(f, 'NOC_MCP_PASSPHRASE=from-repo-env\n'); // secret-scan:allow(fake passphrase fixture for env fallback test)
      const prior = process.env.NOC_MCP_PASSPHRASE;
      process.env.NOC_MCP_DOTENV = f;
      try {
        process.env.NOC_MCP_PASSPHRASE = '';
        ensureRepoEnv();
        expect(process.env.NOC_MCP_PASSPHRASE).toBe('from-repo-env');
        process.env.NOC_MCP_PASSPHRASE = 'harness-supplied'; // secret-scan:allow(fake passphrase fixture for env fallback test)
        ensureRepoEnv();
        expect(process.env.NOC_MCP_PASSPHRASE).toBe('harness-supplied');
      } finally {
        if (prior === undefined) delete process.env.NOC_MCP_PASSPHRASE;
        else process.env.NOC_MCP_PASSPHRASE = prior;
      }
    });

    it('ensureRepoEnv honors NOC_MCP_DOTENV explicit path', () => {
      const f = join(dir, 'custom.env');
      writeFileSync(f, 'NOC_MCP_TEST_C=viacustom\n');
      touched.push('NOC_MCP_TEST_C');
      setForTest('NOC_MCP_TEST_C', undefined);
      process.env.NOC_MCP_DOTENV = f;
      const st = ensureRepoEnv();
      expect(st.path).toBe(f);
      expect(process.env.NOC_MCP_TEST_C).toBe('viacustom');
    });
  });
});
