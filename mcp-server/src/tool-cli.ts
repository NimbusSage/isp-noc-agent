#!/usr/bin/env node
/**
 * Universal tool CLI — harness-independent access to the MCP executor.
 *
 * Problem it fixes: the temp-site executor tools (temp-radio-apply,
 * temp-ospf-apply, temp-mt-flip, …) were only reachable through an MCP
 * client (the OpenCode harness over stdio). Agents running in any other
 * harness — or a plain operator shell — had no way to invoke them, so
 * every temp-connection stalled at "plan only" outside OpenCode.
 *
 * This CLI calls the EXACT same code path as the MCP transport
 * (NocMcpServer.handleToolCall, which parses with the shared tool-schemas
 * table, so injection/format checks are identical), so schemas, the five executor gates,
 * bindings, audits, and redaction behave identically. It is not a second
 * implementation and must never become one: no device logic lives here.
 *
 * Usage:
 *   node dist/tool-cli.js <tool-name> '<json-args>'
 *   node dist/tool-cli.js <tool-name> --args-file /tmp/args.json
 *   echo '<json-args>' | node dist/tool-cli.js <tool-name> --stdin
 *   node dist/tool-cli.js --list
 *
 * Env: same as the MCP server (NOC_MCP_PASSPHRASE, TEMP_SITE_*,
 * LIBRENMS_*, NOC_MCP_CONFIG, NOC_MCP_DOTENV). The repo-root .env
 * fallback (ensureRepoEnv) applies exactly as in index.ts; explicit
 * process env always wins.
 *
 * Exit codes: 0 = tool success; 1 = tool reported isError / transport
 * failure; 2 = CLI usage / JSON parse error. Tool text goes to stdout
 * (pipeable); diagnostics go to stderr. Secrets are never printed.
 */
import { NocMcpServer } from './server.js';
import { ensureRepoEnv } from './config/repo-env.js';
import { ServerConfigSchema, type ServerConfig } from './config/schema.js';
import { readFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoEnv = ensureRepoEnv();
if (repoEnv.path) {
  console.error(`[tool-cli] repo .env fallback: filled ${repoEnv.filled} var(s) from ${repoEnv.path}`);
}

function usage(): string {
  return [
    'Usage:',
    '  node dist/tool-cli.js --list',
    '  node dist/tool-cli.js <tool-name> \'<json-args>\'',
    '  node dist/tool-cli.js <tool-name> --args-file <path>',
    '  echo \'<json-args>\' | node dist/tool-cli.js <tool-name> --stdin',
    '',
    'Examples:',
    '  node dist/tool-cli.js temp-radio-plan \'{"op":"set-mode","incidentId":"INC-F-01\",\"host\":\"10.0.208.10\",\"deviceId\":\"alpha5e\",\"mode\":\"managed\",\"ssid\":\"bravo5w\"}\'',
    '  node dist/tool-cli.js temp-ospf-plan \'{"incidentId":"INC-F-01\",\"host\":\"10.0.122.59\",\"interfaze\":\"ether5\",\"address\":\"10.0.28.97/30\",\"network\":\"10.0.28.96/30\",\"ospfType\":\"ptp\"}\'',
  ].join('\n');
}

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString('utf-8');
}

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  const configPath = process.env.NOC_MCP_CONFIG ?? join(process.cwd(), 'config', 'server.json');
  let config: ServerConfig;
  try {
    config = ServerConfigSchema.parse(JSON.parse(readFileSync(configPath, 'utf-8')));
  } catch {
    config = ServerConfigSchema.parse({});
  }
  // Harness-proofing: server.json ships cwd-relative store/audit paths
  // ("./config/credentials.enc", "./logs/audit.log") which only resolve
  // when cwd is mcp-server/ (the OpenCode launch cwd). Any other harness
  // cwd breaks credential reads and audit writes, so anchor relative paths
  // to the mcp-server root derived from this file's location. When cwd IS
  // mcp-server/ the resolved files are identical — OpenCode behavior unchanged.
  const mcpRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
  for (const section of ['credentialStore', 'auditLog'] as const) {
    const p = config[section].path;
    if (p && !isAbsolute(p)) config[section].path = resolve(mcpRoot, p);
  }
  const nocServer = new NocMcpServer(config);

  if (argv.length === 0 || argv[0] === '-h' || argv[0] === '--help') {
    console.error(usage());
    process.exit(2);
  }
  if (argv[0] === '--list' || argv[0] === 'list') {
    for (const tool of nocServer.getToolDefinitions()) console.log(tool.name);
    return;
  }

  const toolName = argv[0];
  const known = nocServer.getToolDefinitions().some((t) => t.name === toolName);
  if (!known) {
    console.error(`Unknown tool: ${toolName}\n\n${usage()}`);
    process.exit(2);
  }

  let rawArgs: string;
  if (argv[1] === '--stdin') {
    rawArgs = await readStdin();
  } else if (argv[1] === '--args-file') {
    if (!argv[2]) {
      console.error('Missing file path after --args-file\n\n' + usage());
      process.exit(2);
    }
    rawArgs = readFileSync(argv[2], 'utf-8');
  } else if (argv[1]) {
    rawArgs = argv[1];
  } else {
    console.error('Missing JSON args\n\n' + usage());
    process.exit(2);
  }

  let args: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(rawArgs);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('args must be a JSON object');
    args = parsed as Record<string, unknown>;
  } catch (err) {
    console.error(`Invalid JSON args: ${err instanceof Error ? err.message : 'parse error'}`);
    process.exit(2);
  }

  const result = await nocServer.handleToolCall(toolName, args);
  for (const block of result.content) console.log(block.text);
  if (result.isError) process.exit(1);
}

main().catch((err) => {
  console.error(`Fatal error: ${err instanceof Error ? err.message : 'unknown'}`);
  process.exit(1);
});
