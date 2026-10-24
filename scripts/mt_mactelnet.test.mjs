import test from 'node:test';
import assert from 'node:assert/strict';
import { isReadOnly, redactSecrets } from './mt_mactelnet.mjs';

test('isReadOnly allows prints, quits, and bare prompts', () => {
  for (const c of ['/system identity print', '/routing ospf neighbor print', '/ip route print where dst-address=0.0.0.0/0', '/quit', '/']) {
    assert.equal(isReadOnly(c), true, c);
  }
});

test('isReadOnly blocks mutating verbs without --write', () => {
  for (const c of [
    '/interface wireless set wlan1 ssid=x',
    '/ip address add address=10.0.0.1/30 interface=ether5',
    '/routing ospf interface-template remove [find comment~x]',
    '/interface enable ether5',
    '/system reboot',
  ]) {
    assert.equal(isReadOnly(c), false, c);
  }
});

test('isReadOnly: a trailing/leading print never launders a write (chained commands)', () => {
  for (const c of [
    '/system reboot ; /ip address print',
    '/ip address print ; /system reboot',
    '/system identity print;/system reboot',
    '/system identity print\n/system reboot',
    '/system identity print\r\n/system reboot',
    '/ip address print && /system reboot',
    '/ip address print || /system reboot',
    '/ip address add address=1.1.1.1/32 comment=print',
    '/ip address add print',
    '/system script run mine print',
    '/ip/address/add address=1.1.1.1/32',
    '/ / system reboot',
  ]) {
    assert.equal(isReadOnly(c), false, JSON.stringify(c));
  }
});

test('isReadOnly: abbreviated RouterOS verbs are not read verbs', () => {
  for (const c of ['/system reb', '/sys reboot', '/int en ether5', '/ip addr ad address=10.0.0.1/30', '/ip firewall filter rem 0']) {
    assert.equal(isReadOnly(c), false, c);
  }
});

test('isReadOnly: command substitution and scripting are denied', () => {
  for (const c of [
    '/ip address print where comment=[/system reboot]',
    ':put [/ip address print]',
    '/ip address print; :execute {/system reboot}',
    '/ip address print `reboot`',
    '/ip address print $(reboot)',
    ':do { /system reboot }',
    '/system script run x',
  ]) {
    assert.equal(isReadOnly(c), false, c);
  }
});

test('isReadOnly: ordinary read commands (incl. filters, paths, multi-segment) still pass', () => {
  for (const c of [
    '/system identity print',
    '/ip address print where interface=ether5',
    '/ip route print where dst-address=0.0.0.0/0 and active',
    '/ip address print where comment~"set up"',
    '/interface ethernet monitor ether1 once',
    '/ip/address/print',
    '/system resource print; /system clock print',
    '/ip address print detail without-paging',
    '/ping 10.0.0.1 count=3',
    '/quit',
    '/cancel',
    '/',
  ]) {
    assert.equal(isReadOnly(c), true, c);
  }
});

test('isReadOnly: empty or non-string input is refused', () => {
  for (const c of ['', '   ', null, undefined]) assert.equal(isReadOnly(c), false, String(c));
});

test('redactSecrets replaces every secret occurrence', () => {
  const fake = 'fixture-password-1!';
  const out = redactSecrets(`login ${fake} then ${fake} again`, [fake]);
  assert.ok(!out.includes(fake));
  assert.equal(out, 'login <redacted> then <redacted> again');
});
