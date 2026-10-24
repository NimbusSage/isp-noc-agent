import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import type { NocMcpServer } from './server.js';
import { toolSchemas } from './tool-schemas.js';

/**
 * Register every tool the NocMcpServer advertises with the MCP SDK.
 * Fails fast (names the tool) if a tool has no input schema; this is the code
 * path that crashed production startup for routeros-write (see tool-schemas.ts).
 * Kept separate from index.ts so tests can run it without opening stdio.
 */
export function registerTools(mcpServer: McpServer, nocServer: NocMcpServer): string[] {
  const names: string[] = [];
  for (const tool of nocServer.getToolDefinitions()) {
    if (!Object.prototype.hasOwnProperty.call(toolSchemas, tool.name)) {
      throw new Error(`Missing MCP input schema for tool: ${tool.name}`);
    }
    mcpServer.tool(
      tool.name,
      tool.description,
      toolSchemas[tool.name],
      async (args: Record<string, unknown>) => nocServer.handleToolCall(tool.name, args),
    );
    names.push(tool.name);
  }
  return names;
}
