#!/usr/bin/env node
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { NocMcpServer } from './server.js';
import { ensureRepoEnv } from './config/repo-env.js';
import { ServerConfigSchema, type ServerConfig } from './config/schema.js';
import { registerTools } from './register-tools.js';
import { readFileSync } from 'fs';
import { join } from 'path';

// Fill unset vars from the repo-root .env BEFORE anything else reads
// process.env. Explicit env always wins (webhook/systemd path unchanged).
// This is what keeps TUI-launched sessions working without manual exports.
const repoEnv = ensureRepoEnv();
if (repoEnv.path) {
  console.error(`[mcp] repo .env fallback: filled ${repoEnv.filled} var(s) from ${repoEnv.path}`);
} else {
  console.error('[mcp] repo .env fallback: no candidate .env found (NOC_MCP_DOTENV, ./../.env)');
}

const configPath = process.env.NOC_MCP_CONFIG ?? join(process.cwd(), 'config', 'server.json');
let config: ServerConfig;

try {
  const raw = readFileSync(configPath, 'utf-8');
  config = ServerConfigSchema.parse(JSON.parse(raw));
} catch {
  config = ServerConfigSchema.parse({});
}

const nocServer = new NocMcpServer(config);
const mcpServer = new McpServer({
  name: 'noc-network-troubleshooter',
  version: '0.1.0',
});

registerTools(mcpServer, nocServer);

async function main() {
  const transport = new StdioServerTransport();
  await mcpServer.connect(transport);
  console.error('NOC Network Troubleshooter MCP server running on stdio');
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
