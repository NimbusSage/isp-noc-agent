import { readFileSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

const HANDLE = /^[a-z][a-z0-9._:-]{0,40}$/;

/**
 * Who is running this server: NOC_OPERATOR, else the "operator" key of the per-user settings file that
 * `python3 scripts/team.py me <handle>` writes. Undefined on a single-person install. Same rule as
 * scripts/team.py; the value only labels audit records, it grants nothing.
 */
export function resolveOperator(env: NodeJS.ProcessEnv = process.env): string | undefined {
  const fromEnv = (env.NOC_OPERATOR ?? '').trim();
  const candidate = (fromEnv || handleFromUserConfig(env)).toLowerCase();
  return HANDLE.test(candidate) ? candidate : undefined;
}

function handleFromUserConfig(env: NodeJS.ProcessEnv): string {
  const path = (env.NOC_USER_CONFIG ?? '').trim() || join(homedir(), '.config', 'nocagent', 'user.json');
  try {
    const value: unknown = (JSON.parse(readFileSync(path, 'utf-8')) as { operator?: unknown }).operator;
    return typeof value === 'string' ? value.trim() : '';
  } catch {
    return '';
  }
}
