/**
 * Minimal SNMPv2c GET, dependency-free.
 *
 * Why this exists: "only devices that have SNMP should be added to LibreNMS" is
 * a precondition, not a hope. The add path needs to know that the device
 * actually ANSWERS a query with the monitoring community before it touches
 * inventory — adding a silent device just creates a down entry that someone
 * has to clean up later.
 *
 * The connector has no SNMP library, and shelling out to `snmpget` would put the
 * community on a command line (visible in `ps`). So this speaks just enough of
 * BER to send one GetRequest and read one GetResponse:
 *
 *   SEQUENCE { INTEGER version(1=v2c), OCTET STRING community, GetRequest [0] { ... } }
 *
 * Scope is deliberately tiny: one OID (sysName, 1.3.6.1.2.1.1.5.0), UDP, no
 * retries beyond the caller's timeout, no walk, no v3. A UDP timeout is the NORMAL
 * answer from a device with SNMP disabled, so it is reported as `answered:false`,
 * not as an error to escalate.
 */

import { createSocket } from 'node:dgram';

const SYSNAME_OID = [1, 3, 6, 1, 2, 1, 1, 5, 0];

/** Universal tags this module cares about. */
const TAG_INTEGER = 0x02;
const TAG_OCTET_STRING = 0x04;
const TAG_NULL = 0x05;
const TAG_OID = 0x06;
const TAG_SEQUENCE = 0x30;
const TAG_GET_REQUEST = 0xa0;
const TAG_GET_RESPONSE = 0xa2;
/** SNMPv2 exception values: noSuchObject, noSuchInstance, endOfMibView. */
const EXCEPTION_TAGS = [0x80, 0x81, 0x82];

function encodeLength(length: number): Buffer {
  if (length < 0x80) return Buffer.from([length]);
  const bytes: number[] = [];
  let remaining = length;
  while (remaining > 0) {
    bytes.unshift(remaining & 0xff);
    remaining = Math.floor(remaining / 256);
  }
  return Buffer.from([0x80 | bytes.length, ...bytes]);
}

function tlv(tag: number, body: Buffer): Buffer {
  return Buffer.concat([Buffer.from([tag]), encodeLength(body.length), body]);
}

/** BER INTEGER, always non-negative (versions, request ids, error fields). */
function encodeInteger(value: number): Buffer {
  const bytes: number[] = [];
  let remaining = Math.max(0, Math.floor(value));
  do {
    bytes.unshift(remaining & 0xff);
    remaining = Math.floor(remaining / 256);
  } while (remaining > 0);
  if ((bytes[0] ?? 0) & 0x80) bytes.unshift(0);
  return tlv(TAG_INTEGER, Buffer.from(bytes));
}

function encodeOid(parts: number[]): Buffer {
  const first = 40 * (parts[0] ?? 0) + (parts[1] ?? 0);
  const bytes: number[] = [first];
  for (const part of parts.slice(2)) {
    const chunk: number[] = [];
    let remaining = part;
    do {
      chunk.unshift(remaining & 0x7f);
      remaining = Math.floor(remaining / 128);
    } while (remaining > 0);
    for (let i = 0; i < chunk.length - 1; i += 1) chunk[i] |= 0x80;
    bytes.push(...chunk);
  }
  return tlv(TAG_OID, Buffer.from(bytes));
}

/** Encode one GetRequest for sysName. Exported for tests; the community never leaves the buffer. */
export function buildSnmpGet(community: string, requestId: number, oid: number[] = SYSNAME_OID): Buffer {
  const varbind = tlv(TAG_SEQUENCE, Buffer.concat([encodeOid(oid), tlv(TAG_NULL, Buffer.alloc(0))]));
  const pdu = tlv(
    TAG_GET_REQUEST,
    Buffer.concat([encodeInteger(requestId), encodeInteger(0), encodeInteger(0), tlv(TAG_SEQUENCE, varbind)]),
  );
  return tlv(TAG_SEQUENCE, Buffer.concat([encodeInteger(1), tlv(TAG_OCTET_STRING, Buffer.from(community, 'utf8')), pdu]));
}

interface Element {
  tag: number;
  body: Buffer;
  next: number;
}

function readElement(buf: Buffer, offset: number): Element | null {
  if (offset + 2 > buf.length) return null;
  const tag = buf[offset]!;
  const first = buf[offset + 1]!;
  let length = 0;
  let cursor = offset + 2;
  if (first < 0x80) {
    length = first;
  } else {
    const count = first & 0x7f;
    if (count === 0 || count > 4 || cursor + count > buf.length) return null; // no indefinite form in SNMP
    for (let i = 0; i < count; i += 1) length = length * 256 + buf[cursor + i]!;
    cursor += count;
  }
  if (cursor + length > buf.length) return null;
  return { tag, body: buf.subarray(cursor, cursor + length), next: cursor + length };
}

/** Split a constructed value into child elements. */
function readChildren(body: Buffer): Element[] {
  const out: Element[] = [];
  let offset = 0;
  while (offset < body.length) {
    const element = readElement(body, offset);
    if (!element) break;
    out.push(element);
    offset = element.next;
  }
  return out;
}

function readInteger(body: Buffer): number {
  let value = 0;
  for (const byte of body) value = value * 256 + byte;
  return value;
}

export interface SnmpProbeResult {
  /** A valid GetResponse for our request id came back (the device speaks SNMP here). */
  ok: boolean;
  /** Any SNMP packet came back at all. Distinguishes "silent" from "answered with an error". */
  answered: boolean;
  sysName?: string | null;
  error?: string;
}

/**
 * Parse a GetResponse. Returns the sysName when the varbind carries an OCTET
 * STRING; an SNMPv2 exception (noSuchObject / noSuchInstance / endOfMibView) is a
 * valid answer with no value — the device is speaking SNMP, so `ok` stays true and
 * `sysName` is null.
 */
export function parseSnmpGetResponse(packet: Buffer, expectedRequestId?: number): SnmpProbeResult {
  const message = readElement(packet, 0);
  if (!message || message.tag !== TAG_SEQUENCE) return { ok: false, answered: true, error: 'response was not an SNMP SEQUENCE' };
  const parts = readChildren(message.body);
  if (parts.length < 3) return { ok: false, answered: true, error: 'response was truncated' };
  const [version, , pdu] = parts as [Element, Element, Element];
  if (version.tag !== TAG_INTEGER || readInteger(version.body) !== 1) {
    return { ok: false, answered: true, error: 'response was not SNMPv2c' };
  }
  if (pdu.tag !== TAG_GET_RESPONSE) return { ok: false, answered: true, error: `unexpected PDU tag 0x${pdu.tag.toString(16)}` };
  const fields = readChildren(pdu.body);
  if (fields.length < 4) return { ok: false, answered: true, error: 'response PDU was truncated' };
  const [requestId, errorStatus, errorIndex, varbinds] = fields as [Element, Element, Element, Element];
  if (expectedRequestId !== undefined && readInteger(requestId.body) !== expectedRequestId) {
    return { ok: false, answered: true, error: 'response request-id did not match the request' };
  }
  const status = readInteger(errorStatus.body);
  if (status !== 0) {
    return { ok: false, answered: true, error: `agent returned error-status ${status} at index ${readInteger(errorIndex.body)}` };
  }
  const first = readChildren(varbinds.body)[0];
  if (!first) return { ok: false, answered: true, error: 'response carried no varbind' };
  const value = readChildren(first.body)[1];
  if (!value) return { ok: false, answered: true, error: 'varbind carried no value' };
  if (value.tag === TAG_OCTET_STRING) return { ok: true, answered: true, sysName: value.body.toString('utf8') };
  if (EXCEPTION_TAGS.includes(value.tag) || value.tag === TAG_NULL) return { ok: true, answered: true, sysName: null };
  if (value.tag === TAG_INTEGER && [1, 2, 5].includes(readInteger(value.body))) {
    return { ok: true, answered: true, sysName: null }; // SNMPv1-style noSuchName answers
  }
  return { ok: false, answered: true, error: `unexpected varbind type 0x${value.tag.toString(16)}` };
}

export interface SnmpProbeOptions {
  port?: number;
  timeoutMs?: number;
  /** Fixed request id for tests; random otherwise. */
  requestId?: number;
}

/**
 * One SNMPv2c GET for sysName. Never throws: a silent device, a refused socket
 * and a malformed reply are all results the caller must report as "not proven
 * SNMP" rather than as an outage.
 */
export function snmpGetSysName(host: string, community: string, options: SnmpProbeOptions = {}): Promise<SnmpProbeResult> {
  const port = options.port ?? 161;
  const timeoutMs = options.timeoutMs ?? 3000;
  const requestId = options.requestId ?? Math.floor(Math.random() * 0x7ffffff0) + 1;
  const payload = buildSnmpGet(community, requestId);

  return new Promise<SnmpProbeResult>((resolve) => {
    const socket = createSocket({ type: 'udp4' });
    let settled = false;
    const finish = (result: SnmpProbeResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { socket.close(); } catch { /* already closed */ }
      resolve(result);
    };
    const timer = setTimeout(
      () => finish({ ok: false, answered: false, error: `no SNMP response within ${timeoutMs}ms` }),
      timeoutMs,
    );
    socket.on('message', (message) => finish(parseSnmpGetResponse(message, requestId)));
    socket.on('error', (error: Error) => finish({ ok: false, answered: false, error: error.message }));
    socket.send(payload, port, host, (error) => {
      if (error) finish({ ok: false, answered: false, error: error.message });
    });
  });
}
