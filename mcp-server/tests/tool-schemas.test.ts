import { describe, it, expect, afterEach } from 'vitest';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js';
import { NocMcpServer } from '../src/server.js';
import { registerTools } from '../src/register-tools.js';
import { toolSchemas, parseToolArgs } from '../src/tool-schemas.js';
import { ServerConfigSchema } from '../src/config/schema.js';

const dirs: string[] = [];
function makeServer(inventoryEnabled = true): NocMcpServer {
  const dir = mkdtempSync(join(tmpdir(), 'noc-tools-'));
  dirs.push(dir);
  return new NocMcpServer(ServerConfigSchema.parse({
    auditLog: { path: join(dir, 'audit.log') },
    credentialStore: { path: join(dir, 'creds.enc'), passphraseEnvVar: 'NOC_TEST_UNSET_PASSPHRASE' },
    inventory: { enabled: inventoryEnabled },
  }));
}
afterEach(() => {
  while (dirs.length) rmSync(dirs.pop() as string, { recursive: true, force: true });
});

describe('P0 regression: every registered tool has an input schema', () => {
  it('has a schema for every tool the server advertises (routeros-write was missing)', () => {
    for (const inventory of [true, false]) {
      for (const tool of makeServer(inventory).getToolDefinitions()) {
        expect(Object.keys(toolSchemas), `missing input schema for ${tool.name}`).toContain(tool.name);
      }
    }
    expect(Object.keys(toolSchemas)).toContain('routeros-write');
  });

  it('has no orphan schemas for tools the server does not advertise', () => {
    const advertised = new Set(makeServer(true).getToolDefinitions().map((t) => t.name));
    for (const name of Object.keys(toolSchemas)) expect(advertised.has(name), `orphan schema ${name}`).toBe(true);
  });

  it('registerTools (the startup path index.ts runs) does not throw and registers all tools', () => {
    const noc = makeServer(true);
    const mcp = new McpServer({ name: 'test', version: '0.0.0' });
    const names = registerTools(mcp, noc);
    expect(names).toEqual(noc.getToolDefinitions().map((t) => t.name));
    expect(names).toContain('routeros-write');
    expect(names).toHaveLength(17);
  });

  it('registerTools fails fast, naming the tool, when a schema is missing', () => {
    const noc = makeServer(true);
    const original = toolSchemas['routeros-write'];
    delete toolSchemas['routeros-write'];
    try {
      expect(() => registerTools(new McpServer({ name: 't', version: '0' }), noc)).toThrow(/Missing MCP input schema for tool: routeros-write/);
    } finally {
      toolSchemas['routeros-write'] = original;
    }
  });

  it('serves all tools over a real MCP transport and validates routeros-write input', async () => {
    const noc = makeServer(true);
    const mcp = new McpServer({ name: 'test', version: '0.0.0' });
    registerTools(mcp, noc);
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    const client = new Client({ name: 'c', version: '0.0.0' });
    await Promise.all([mcp.connect(serverSide), client.connect(clientSide)]);
    try {
      const listed = await client.listTools();
      expect(listed.tools.map((t) => t.name)).toContain('routeros-write');
      expect(listed.tools).toHaveLength(17);

      // Dry-run plan works through the transport (no gates, no network).
      const ok = await client.callTool({
        name: 'routeros-write',
        arguments: { op: 'poe-off', host: '10.0.0.5', deviceId: 'sw-1', interfaze: 'ether3', reason: 'hung radio needs a power cycle', execute: false },
      });
      expect(ok.isError).toBeFalsy();
      expect(JSON.stringify(ok.content)).toContain('poe-out=off');

      // Injection attempts are rejected by the schema before any handler runs.
      for (const bad of [
        { interfaze: 'ether3" ; /user add name=x' },
        { interfaze: 'ether3\n/system reboot' },
        { interfaze: 'ether[find]' },
        { host: '010.0.0.5' },
        { host: '10.0.0.5; reboot' },
      ]) {
        const res = await client.callTool({
          name: 'routeros-write',
          arguments: { op: 'poe-off', host: '10.0.0.5', deviceId: 'sw-1', interfaze: 'ether3', reason: 'hung radio needs a power cycle', execute: false, ...bad },
        });
        expect(res.isError, JSON.stringify(bad)).toBe(true);
      }
    } finally {
      await client.close();
      await mcp.close();
    }
  });
});

describe('parseToolArgs (shared by MCP transport and tool-cli)', () => {
  it('rejects unknown tools and malformed args', () => {
    expect(parseToolArgs('nope', {}).ok).toBe(false);
    expect(parseToolArgs('run-diagnostics', { host: '', deviceType: 'mikrotik-ssh', command: 'ping' }).ok).toBe(false);
    expect(parseToolArgs('run-diagnostics', { host: '8.8.8.8;reboot', deviceType: 'mikrotik-ssh', command: 'ping' }).ok).toBe(false);
  });

  it('applies defaults the same way the SDK does', () => {
    const r = parseToolArgs('run-diagnostics', { host: '10.0.0.1', deviceType: 'mikrotik-ssh', command: 'system-resource' });
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.data.params).toEqual({});
    const apply = parseToolArgs('temp-mt-bounce', { incidentId: 'INC-1', host: '10.0.0.1', deviceId: 'd' });
    expect(apply.ok && apply.data.execute).toBe(false);
  });
});
