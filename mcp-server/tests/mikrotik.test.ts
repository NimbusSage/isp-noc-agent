import { describe, it, expect, vi, beforeEach } from 'vitest';
import { MikroTikClient } from '../src/devices/mikrotik.js';
import { MIKROTIK_SSH_COMMANDS } from '../src/commands/allowlist.js';

const mockEnd = vi.fn();
const mockExec = vi.fn();

vi.mock('ssh2', () => ({
  Client: vi.fn(() => {
    const handlers: Record<string, (...args: unknown[]) => void> = {};
    return {
      on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
        handlers[event] = handler;
      }),
      connect: vi.fn(() => {
        if (handlers['ready']) handlers['ready']();
      }),
      exec: mockExec,
      end: mockEnd,
    };
  }),
}));

describe('MikroTikClient', () => {
  let client: MikroTikClient;

  beforeEach(() => {
    vi.clearAllMocks();
    mockExec.mockImplementation((_cmd: string, cb: (err: unknown, stream: Record<string, unknown>) => void) => {
      const streamHandlers: Record<string, (...args: unknown[]) => void> = {};
      const stream = {
        on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
          streamHandlers[event] = handler;
          if (event === 'data') handler(Buffer.from('uptime: 1 day\n cpu: 5%\n'));
          if (event === 'close') handler(0);
        }),
        pipe: vi.fn(),
        write: vi.fn(),
        end: vi.fn(),
      };
      cb(null, stream);
    });
    client = new MikroTikClient('192.168.1.1', 22, {
      name: 'primary',
      username: 'admin',
      password: 'pass',
      priority: 0,
      tags: [],
    });
  });

  it('constructs command correctly', () => {
    const cmd = client.buildCommand(MIKROTIK_SSH_COMMANDS['system-resource'], {});
    expect(cmd).toBe('/system resource print');
  });

  it('substitutes parameters in command template', () => {
    const cmd = client.buildCommand(MIKROTIK_SSH_COMMANDS['ping'], { address: '8.8.8.8', count: 5 });
    expect(cmd).toContain('address=8.8.8.8');
    expect(cmd).toContain('count=5');
  });

  it('returns structured result', async () => {
    const result = await client.execute(MIKROTIK_SSH_COMMANDS['system-resource'], {});
    expect(result.device).toBe('192.168.1.1');
    expect(result.command).toBe('system-resource');
    expect(result.status).toBe('success');
    expect(result.result).toBeDefined();
  });

  it('enforces command timeout', async () => {
    mockExec.mockImplementation((_cmd: string, cb: (err: unknown, stream: Record<string, unknown>) => void) => {
      const stream = {
        on: vi.fn(),
        pipe: vi.fn(),
        write: vi.fn(),
        end: vi.fn(),
      };
      cb(null, stream);
    });
    const slowClient = new MikroTikClient('192.168.1.2', 22, {
      name: 'slow',
      username: 'admin',
      password: 'pass',
      priority: 0,
      tags: [],
    }, 100);

    await slowClient.connect();
    const result = await slowClient.execute(MIKROTIK_SSH_COMMANDS['system-resource'], {});
    expect(result.status).toBe('timeout');
    expect(result.warnings[0]).toContain('timed out');
  }, 10000);

  it('handles disconnect errors gracefully', async () => {
    mockEnd.mockImplementation(() => {
      throw new Error('Connection already closed');
    });
    const errorClient = new MikroTikClient('192.168.1.3', 22, {
      name: 'error',
      username: 'admin',
      password: 'pass',
      priority: 0,
      tags: [],
    });

    await errorClient.connect();
    await expect(errorClient.disconnect()).resolves.not.toThrow();
  });
});
