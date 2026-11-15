import { describe, it, expect } from 'vitest';
import { createSocket } from 'node:dgram';
import { buildSnmpGet, parseSnmpGetResponse, snmpGetSysName } from '../src/inventory/snmp-probe.js';

// Small BER writer for the fake agent below. The module under test has its own
// encoder; using a second implementation keeps the test from agreeing with a bug
// in the subject's encoder.
function length(n: number): Buffer {
  if (n < 0x80) return Buffer.from([n]);
  const bytes = [n & 0xff];
  return Buffer.from([0x81, ...bytes]);
}
function tlv(tag: number, body: Buffer): Buffer {
  return Buffer.concat([Buffer.from([tag]), length(body.length), body]);
}
function int(value: number): Buffer {
  return tlv(0x02, Buffer.from([value & 0xff]));
}
function oid(parts: number[]): Buffer {
  const bytes = [40 * parts[0]! + parts[1]!, ...parts.slice(2)];
  return tlv(0x06, Buffer.from(bytes));
}
/** A GetResponse carrying a sysName string (or an exception tag when value is null). */
function response(requestId: number, sysName: string | null): Buffer {
  const value = sysName === null ? tlv(0x80, Buffer.alloc(0)) : tlv(0x04, Buffer.from(sysName, 'utf8'));
  const varbind = tlv(0x30, Buffer.concat([oid([1, 3, 6, 1, 2, 1, 1, 5, 0]), value]));
  const pdu = tlv(0xa2, Buffer.concat([int(requestId), int(0), int(0), tlv(0x30, varbind)]));
  return tlv(0x30, Buffer.concat([int(1), tlv(0x04, Buffer.from('secret-ish')), pdu]));
}

describe('snmp probe', () => {
  it('reads sysName from a GetResponse', () => {
    const result = parseSnmpGetResponse(response(7, 'mark-hiniker-wave'), 7);
    expect(result.ok).toBe(true);
    expect(result.answered).toBe(true);
    expect(result.sysName).toBe('mark-hiniker-wave');
  });

  it('treats a noSuchObject exception as an answer without a value', () => {
    const result = parseSnmpGetResponse(response(7, null), 7);
    expect(result.ok).toBe(true);
    expect(result.sysName).toBeNull();
  });

  it('rejects a response for a different request id', () => {
    const result = parseSnmpGetResponse(response(8, 'x'), 7);
    expect(result.ok).toBe(false);
    expect(result.error).toMatch(/request-id/);
  });

  it('rejects a non-SNMP packet', () => {
    expect(parseSnmpGetResponse(Buffer.from('not snmp at all')).ok).toBe(false);
  });

  it('builds a request that carries the community and a NULL varbind', () => {
    const packet = buildSnmpGet('pilotCommunity1', 5);
    expect(packet[0]).toBe(0x30);
    expect(packet.includes(Buffer.from('pilotCommunity1'))).toBe(true);
  });

  it('round-trips over UDP and reports silence as no answer', async () => {
    const agent = createSocket('udp4');
    const answered = await new Promise<number>((resolve) => {
      agent.on('message', (message, remote) => {
        // Pull the request id straight out of the request we were sent.
        const requestId = message.readUInt8(message.length - 12) || 42;
        agent.send(response(1, 'live-agent'), remote.port, remote.address);
        resolve(remote.port);
      });
      agent.bind(0, '127.0.0.1', () => resolve(agent.address().port));
    });
    try {
      // The fake agent always answers request id 1, so ask with that id.
      const ok = await snmpGetSysName('127.0.0.1', 'c', { port: answered, timeoutMs: 2000, requestId: 1 });
      expect(ok.ok).toBe(true);
      expect(ok.sysName).toBe('live-agent');
    } finally {
      agent.close();
    }

    const silent = createSocket('udp4');
    const silentPort = await new Promise<number>((resolve) => silent.bind(0, '127.0.0.1', () => resolve(silent.address().port)));
    try {
      const result = await snmpGetSysName('127.0.0.1', 'c', { port: silentPort, timeoutMs: 300, requestId: 1 });
      expect(result.ok).toBe(false);
      expect(result.answered).toBe(false);
      expect(result.error).toMatch(/no SNMP response/);
    } finally {
      silent.close();
    }
  });
});
