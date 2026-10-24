/**
 * Repo-root .env fallback loader.
 *
 * Root cause it fixes: the MCP server is spawned by OpenCode with
 * `{env:VAR}` expansion from the OpenCode process environment. The webhook
 * service runs under systemd with `EnvironmentFile=.env`, so every gate var
 * is present there — but an operator TUI session launched from a bare shell
 * has none of them, and EVERY MCP tool then fails closed with
 * "Missing passphrase" (or silently missing TEMP_SITE and LIBRENMS gates).
 *
 * This loader fills in only variables that are currently unset/empty from
 * the first readable candidate file. Explicit process environment always
 * wins, so webhook/systemd behavior is unchanged.
 *
 * Security: values are never logged. Only the file path and the count of
 * filled variables are reported on stderr (stdout is the MCP protocol).
 */
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

export interface RepoEnvStatus {
  path: string | null;
  filled: number;
}

let lastStatus: RepoEnvStatus = { path: null, filled: 0 };

export function getRepoEnvStatus(): RepoEnvStatus {
  return lastStatus;
}

/** Minimal .env parser: KEY=VALUE, optional `export `, # comments, quotes. */
export function parseDotEnv(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const body = line.startsWith('export ') ? line.slice(7).trimStart() : line;
    const eq = body.indexOf('=');
    if (eq <= 0) continue;
    const key = body.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    let val = body.slice(eq + 1).trim();
    if (val.length >= 2 && val.startsWith('"') && val.endsWith('"')) {
      val = val
        .slice(1, -1)
        .replace(/\\n/g, '\n')
        .replace(/\\"/g, '"')
        .replace(/\\\\/g, '\\');
    } else if (val.length >= 2 && val.startsWith("'") && val.endsWith("'")) {
      val = val.slice(1, -1);
    } else {
      // Unquoted: strip trailing inline comment, keep everything else verbatim.
      const hash = val.indexOf(' #');
      if (hash >= 0) val = val.slice(0, hash).trimEnd();
    }
    out[key] = val;
  }
  return out;
}

/**
 * Load one file; returns number of variables filled. A variable counts as
 * UNSET when it is undefined OR the empty string: OpenCode's `{env:X}`
 * interpolation yields "" (not undefined) when X is absent from the agent's
 * environment, and the server-side lane relies on that to fall through to the
 * repo .env. A non-empty explicit value always wins.
 */
export function loadDotEnvFile(path: string): number {
  const parsed = parseDotEnv(readFileSync(path, 'utf-8'));
  let filled = 0;
  for (const [key, val] of Object.entries(parsed)) {
    if (process.env[key] === undefined || process.env[key] === '') {
      process.env[key] = val;
      filled += 1;
    }
  }
  return filled;
}

function candidatePaths(): string[] {
  const explicit = process.env.NOC_MCP_DOTENV;
  const paths = [
    ...(explicit ? [explicit] : []),
    join(process.cwd(), '.env'),
    join(process.cwd(), '..', '.env'),
  ];
  return [...new Set(paths)];
}

/**
 * Fill unset vars from the first readable candidate .env. Idempotent and
 * safe to call at startup before any other module reads process.env.
 */
export function ensureRepoEnv(): RepoEnvStatus {
  for (const path of candidatePaths()) {
    try {
      if (!existsSync(path)) continue;
      const filled = loadDotEnvFile(path);
      lastStatus = { path, filled };
      return lastStatus;
    } catch {
      continue;
    }
  }
  lastStatus = { path: null, filled: 0 };
  return lastStatus;
}
