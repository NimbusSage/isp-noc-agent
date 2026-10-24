import type { DeviceCommand, ParamSpec } from '../devices/types.js';
import { hostSyntaxError } from '../security/hosts.js';
import { UISP_DEVICE_ID_RE } from '../security/validators.js';

/*
 * Per-parameter schemas (Lane 2b). Every string parameter pasted into a device
 * command or URL has an explicit closed format; the validator additionally
 * rejects ; newline [ ] $ " ' ` \ | and control characters in ANY string.
 */
const ADDRESS_PARAM: ParamSpec = {
  type: 'string', required: true, maxLength: 253,
  check: (v) => hostSyntaxError(v),
};
const INTERFACE_PARAM: ParamSpec = {
  type: 'string', required: true, maxLength: 32,
  pattern: /^[A-Za-z0-9_.:-]{1,32}$/,
};
const COUNT_PARAM = (dflt: number): ParamSpec => ({
  type: 'number', required: false, default: dflt, integer: true, min: 1, max: 20,
});
// bandwidth-test credentials: the RouterOS password is pasted unquoted, so only
// characters with no RouterOS meaning outside quotes are accepted.
const BTEST_USER_PARAM: ParamSpec = { type: 'string', required: true, maxLength: 64, pattern: /^[A-Za-z0-9_.@-]{1,64}$/ };
const BTEST_PASSWORD_PARAM: ParamSpec = { type: 'string', required: true, maxLength: 128, pattern: /^[A-Za-z0-9_.@%+=:,/!#^*~-]{1,128}$/ };
const UISP_DEVICE_ID_PARAM: ParamSpec = { type: 'string', required: true, maxLength: 128, pattern: UISP_DEVICE_ID_RE };
// UISP statistics interval (HOUR, DAY, WEEK, ...): letters/digits/underscore only.
const UISP_INTERVAL_PARAM: ParamSpec = { type: 'string', required: false, default: 'HOUR', maxLength: 16, pattern: /^[A-Za-z0-9_]{1,16}$/ };

export const MIKROTIK_SSH_COMMANDS: Record<string, DeviceCommand> = {
  'system-resource': {
    name: 'system-resource',
    category: 'system',
    description: 'CPU, memory, disk, uptime, RouterOS version',
    template: '/system resource print',
    params: {},
    readOnly: true,
  },
  'system-clock': {
    name: 'system-clock',
    category: 'system',
    description: 'Current date/time and timezone',
    template: '/system clock print',
    params: {},
    readOnly: true,
  },
  'interface-list': {
    name: 'interface-list',
    category: 'interfaces',
    description: 'All interfaces with traffic stats',
    template: '/interface print stats',
    params: {},
    readOnly: true,
  },
  'interface-monitor': {
    name: 'interface-monitor',
    category: 'interfaces',
    description: 'Real-time traffic rates for interface',
    template: '/interface monitor-traffic [find name={{interface}}] once',
    params: {
      interface: INTERFACE_PARAM,
    },
    readOnly: true,
  },
  'interface-ethernet-monitor': {
    name: 'interface-ethernet-monitor',
    category: 'interfaces',
    description: 'PHY status (speed, duplex, link)',
    template: '/interface ethernet monitor [find name={{interface}}] once',
    params: {
      interface: INTERFACE_PARAM,
    },
    readOnly: true,
  },
  'ip-route': {
    name: 'ip-route',
    category: 'network',
    description: 'Routing table',
    template: '/ip route print',
    params: {},
    readOnly: true,
  },
  'ip-address': {
    name: 'ip-address',
    category: 'network',
    description: 'IP address assignments',
    template: '/ip address print',
    params: {},
    readOnly: true,
  },
  'ip-arp': {
    name: 'ip-arp',
    category: 'network',
    description: 'ARP table',
    template: '/ip arp print',
    params: {},
    readOnly: true,
  },
  'ip-dns': {
    name: 'ip-dns',
    category: 'network',
    description: 'DNS settings and cache',
    template: '/ip dns print',
    params: {},
    readOnly: true,
  },
  'ip-dhcp-lease': {
    name: 'ip-dhcp-lease',
    category: 'network',
    description: 'Active DHCP leases',
    template: '/ip dhcp-server lease print',
    params: {},
    readOnly: true,
  },
  'ppp-active': {
    name: 'ppp-active',
    category: 'network',
    description: 'Active PPPoE sessions (user, MAC, address) for CPE IP resolution during site renames',
    template: '/ppp active print',
    params: {},
    readOnly: true,
  },
  'firewall-filter': {
    name: 'firewall-filter',
    category: 'firewall',
    description: 'Firewall filter rules with hit counts',
    template: '/ip firewall filter print stats',
    params: {},
    readOnly: true,
  },
  'firewall-connections': {
    name: 'firewall-connections',
    category: 'firewall',
    description: 'Active firewall connections',
    template: '/ip firewall connection print',
    params: {},
    readOnly: true,
  },
  'log-recent': {
    name: 'log-recent',
    category: 'system',
    description: 'Recent system log entries',
    template: '/log print without-paging',
    params: {},
    readOnly: true,
  },
  'ping': {
    name: 'ping',
    category: 'diagnostic',
    description: 'ICMP ping from device',
    template: '/tool ping address={{address}} count={{count}}',
    params: {
      address: ADDRESS_PARAM,
      count: COUNT_PARAM(3),
    },
    readOnly: true,
  },
  'tool-traceroute': {
    name: 'tool-traceroute',
    category: 'diagnostic',
    description: 'Traceroute from device to internet (verified live: /tool traceroute address=8.8.8.8 count=1 use-dns=no)',
    template: '/tool traceroute address={{address}} count={{count}} use-dns=no',
    params: {
      address: ADDRESS_PARAM,
      count: COUNT_PARAM(1),
    },
    readOnly: true,
  },
  'bandwidth-test': {
    name: 'bandwidth-test',
    category: 'diagnostic',
    description: 'MikroTik bandwidth-test to fiber router (user/password required, same as mikrotik-ssh creds; tcp 10s both verified)',
    template: '/tool bandwidth-test address={{address}} protocol={{protocol}} duration={{duration}} direction={{direction}} user={{user}} password={{password}}',
    params: {
      address: ADDRESS_PARAM,
      protocol: { type: 'string', required: false, default: 'tcp', enum: ['tcp', 'udp'] },
      // The command timeout is 30s by default, so longer tests could never finish.
      duration: { type: 'number', required: false, default: 10, integer: true, min: 1, max: 30 },
      direction: { type: 'string', required: false, default: 'both', enum: ['both', 'send', 'receive'] },
      user: BTEST_USER_PARAM,
      password: BTEST_PASSWORD_PARAM,
    },
    readOnly: true,
  },
  'routing-ospf-neighbor': {
    name: 'routing-ospf-neighbor',
    category: 'network',
    description: 'OSPF neighbor table to find /30 peer IPs (verified: /routing ospf neighbor print)',
    template: '/routing ospf neighbor print',
    params: {},
    readOnly: true,
  },
  'routing-bgp-connection': {
    name: 'routing-bgp-connection',
    category: 'network',
    description: 'BGP connection table (RouterOS7: /routing/bgp/connection print, replaces peer print)',
    template: '/routing/bgp/connection print',
    params: {},
    readOnly: true,
  },
  'ip-route-active': {
    name: 'ip-route-active',
    category: 'network',
    description: 'Active routes for multipath detection (dst 0.0.0.0/0 active gateway count)',
    template: '/ip route print where active',
    params: {},
    readOnly: true,
  },
  'wireless-registration-table': {
    name: 'wireless-registration-table',
    category: 'wireless',
    description: 'MikroTik wireless registration table for link health (signal, CCQ, tx-rate)',
    template: '/interface wireless registration-table print detail',
    params: {},
    readOnly: true,
  },
};

export const MIKROTIK_REST_COMMANDS: Record<string, DeviceCommand> = {
  'system-resource': {
    ...MIKROTIK_SSH_COMMANDS['system-resource'],
    template: '/system/resource/print',
  },
  'system-clock': {
    ...MIKROTIK_SSH_COMMANDS['system-clock'],
    template: '/system/clock/print',
  },
  'interface-list': {
    ...MIKROTIK_SSH_COMMANDS['interface-list'],
    template: '/interface/print',
  },
  'ip-route': {
    ...MIKROTIK_SSH_COMMANDS['ip-route'],
    template: '/ip/route/print',
  },
  'ip-address': {
    ...MIKROTIK_SSH_COMMANDS['ip-address'],
    template: '/ip/address/print',
  },
  'firewall-filter': {
    ...MIKROTIK_SSH_COMMANDS['firewall-filter'],
    template: '/ip/firewall/filter/print',
  },
};

export const UBNT_UISP_COMMANDS: Record<string, DeviceCommand> = {
  'device-list': {
    name: 'device-list',
    category: 'system',
    description: 'List all managed devices',
    template: '/nms/api/v2.1/devices',
    params: {},
    readOnly: true,
  },
  'device-detail': {
    name: 'device-detail',
    category: 'system',
    description: 'Full device details including radio and wireless',
    template: '/nms/api/v2.1/devices/{{deviceId}}/detail',
    params: {
      deviceId: UISP_DEVICE_ID_PARAM,
    },
    readOnly: true,
  },
  'device-statistics': {
    name: 'device-statistics',
    category: 'interfaces',
    description: 'Device throughput and error statistics',
    template: '/nms/api/v2.1/devices/{{deviceId}}/statistics',
    params: {
      deviceId: UISP_DEVICE_ID_PARAM,
      interval: UISP_INTERVAL_PARAM,
    },
    readOnly: true,
  },
  'device-interfaces': {
    name: 'device-interfaces',
    category: 'interfaces',
    description: 'Interface status and config',
    template: '/nms/api/v2.1/devices/{{deviceId}}/interfaces',
    params: {
      deviceId: UISP_DEVICE_ID_PARAM,
    },
    readOnly: true,
  },
  'alerts': {
    name: 'alerts',
    category: 'system',
    description: 'Active system alerts',
    template: '/nms/api/v2.1/alerts',
    params: {},
    readOnly: true,
  },
  'sites': {
    name: 'sites',
    category: 'system',
    description: 'List all sites',
    template: '/nms/api/v2.1/sites',
    params: {},
    readOnly: true,
  },
};

export const UBNT_AEROS_COMMANDS: Record<string, DeviceCommand> = {
  'status': {
    name: 'status',
    category: 'system',
    description: 'Full device status (JSON)',
    template: '/status.cgi',
    params: {},
    readOnly: true,
  },
  'stations': {
    name: 'stations',
    category: 'wireless',
    description: 'Connected stations list',
    template: '/sta.cgi',
    params: {},
    readOnly: true,
  },
  'interfaces': {
    name: 'interfaces',
    category: 'interfaces',
    description: 'Interface stats',
    template: '/ifstats.cgi',
    params: {},
    readOnly: true,
  },
  'interface-list': {
    name: 'interface-list',
    category: 'interfaces',
    description: 'Interfaces with status',
    template: '/iflist.cgi',
    params: {},
    readOnly: true,
  },
  'board-info': {
    name: 'board-info',
    category: 'system',
    description: 'Board hardware info',
    template: '/getboardinfo.sh',
    params: {},
    readOnly: true,
  },
};

export const UBNT_WAVE_COMMANDS: Record<string, DeviceCommand> = {
  'ucfg-show': {
    name: 'ucfg-show',
    category: 'system',
    description: 'Full WaveOS JSON config (system.hostname, wireless.ssid, network gateway)',
    template: 'ucfg show',
    params: {},
    readOnly: true,
  },
  'assoc-stations': {
    name: 'assoc-stations',
    category: 'wireless',
    description: 'AP association hierarchy + traffic counters (reassoc proof after rename)',
    template: 'apstats -R',
    params: {},
    readOnly: true,
  },
  'iwconfig': {
    name: 'iwconfig',
    category: 'wireless',
    description: 'Broadcast ESSID readback (independent of ucfg)',
    template: 'iwconfig',
    params: {},
    readOnly: true,
  },
};

export const COMMAND_REGISTRY: Record<string, Record<string, DeviceCommand>> = {
  'mikrotik-ssh': MIKROTIK_SSH_COMMANDS,
  'mikrotik-rest': MIKROTIK_REST_COMMANDS,
  'ubnt-uisp': UBNT_UISP_COMMANDS,
  'ubnt-airos': UBNT_AEROS_COMMANDS,
  'ubnt-wave': UBNT_WAVE_COMMANDS,
  // Isolated customer/CPE pools (rename-site): same read-only commands as
  // their infra counterparts, but separate credential pools so CPE passwords
  // never mix with infrastructure credentials.
  'customer-airos': UBNT_AEROS_COMMANDS,
  'customer-mt': MIKROTIK_SSH_COMMANDS,
};
