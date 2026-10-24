#!/usr/bin/env node
/**
 * Harness-neutral stdio launcher for the NOC MCP server.
 *
 * Why it exists: mcp-server/dist/index.js resolves config/server.json, the
 * encrypted credential store (./config/credentials.enc), the audit log and
 * the repo .env fallback relative to process.cwd(), so it only works when
 * the process starts with cwd = mcp-server/. OpenCode, Codex, Gemini CLI and
 * VS Code can set a working directory in their MCP registration. Claude Code
 * and Cursor document no such field, so they register this launcher instead:
 * it anchors the working directory from its own location and then hands the
 * process to the real server. No logic lives here.
 *
 * Usage (any MCP client, any cwd):  node <repo>/scripts/mcp_launch.mjs
 * Secrets: none are read or passed here. The server loads the repo .env
 * itself (ensureRepoEnv); a harness may pass variables through by name.
 */
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const mcpRoot = resolve(repoRoot, 'mcp-server');
const entry = resolve(mcpRoot, 'dist', 'index.js');

if (!existsSync(entry)) {
  // stdout is the MCP protocol channel; diagnostics go to stderr only.
  console.error(
    `[mcp-launch] ${entry} not found. Build it: cd mcp-server && npm ci && npm run build`,
  );
  process.exit(1);
}

process.chdir(mcpRoot);
await import(pathToFileURL(entry).href);
