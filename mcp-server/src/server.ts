import { z } from 'zod/v3';
import { ServerConfigSchema, type ServerConfig } from './config/schema.js';
import { getRepoEnvStatus } from './config/repo-env.js';
import { CredentialStore } from './credentials/store.js';
import type { CredentialStore as CredentialStoreData } from './credentials/schema.js';
import { CredentialResolver } from './credentials/resolver.js';
import { CommandValidator } from './commands/validator.js';
import { createDeviceClient } from './devices/factory.js';
import { RedactionEngine } from './redaction/engine.js';
import { AuditLogger, type AuditEvent } from './audit/logger.js';
import { resolveOperator } from './audit/operator.js';
import { lookupLibreNmsDeviceByExactIp } from './inventory/librenms.js';
import { LibrenmsAddSchema, postLibrenmsDevice, librenmsAddRequestPreview } from './inventory/librenms-add.js';
import { snmpGetSysName } from './inventory/snmp-probe.js';
import { RouterOsWriteSchema, planRouterOsWrite, executeRouterOsWrite, checkInfraWriteGates } from './writes/routeros-writes.js';
import {
  SnmpConfigSchema,
  planSnmpConfig,
  redactPlan,
  redactCommunity,
  redactVerifyOutput,
  readSnmpSettings,
  checkSnmpWriteGates,
  executeMikrotikSnmp,
  executeWaveSnmp,
  waveSnmpOnlyView,
  poolCredentialsEnabled,
  poolCredentialTries,
  interleavePoolCandidates,
  VENDOR_DEVICE_TYPES,
  type SnmpConfig,
  type SnmpPlan,
  type SnmpSettings,
} from './writes/snmp-config.js';
import { RouterOsTempAccessSchema, planRouterOsTempAccess, executeRouterOsTempAccess, checkTempAccessGates, type RouterOsTempAccess } from './writes/routeros-temp-access.js';
import { planTempOspf } from './temp-site/ospf.js';
import { planTempRadio, checkExecuteGates, checkTransportGates, opToEdits, resolveStationPsk, redactCfgValues, readbackCommand } from './temp-site/radio.js';
import { TempOspfApplySchema, applyOspfAdjacency, rollbackOspfAdjacency, checkRouterGates, TempMtFlipSchema, applyMtFlip, rollbackMtFlip, checkMtGates, TempApWdsSchema, applyApWds, TempMtBounceSchema, applyMtBounce, TempMtAliasSchema, applyMtAlias, removeMtAlias } from './temp-site/mikrotik.js';
import { MikroTikClient } from './devices/mikrotik.js';
import { Ssh2Runner, applyCfgEdits, restoreSnapshot } from './temp-site/airos-ssh.js';
import { parseToolArgs, runDiagnosticsShape, runExactDeviceDiagnosticsShape, toolSchemas } from './tool-schemas.js';
import { checkReadHost, checkWriteHost, hostEnvProblems } from './security/hosts.js';
import { stripControl } from './security/validators.js';
import { setHostKeyEventSink } from './security/hostkeys.js';
import { readSnapshotFile, removeSnapshotFile, snapshotName, writeSnapshotFile } from './temp-site/snapshots.js';
import type { DeviceTypeCredential } from './credentials/schema.js';
import type { DeviceCommand, CommandResult } from './devices/types.js';

// The schemas below are derived from the SAME shapes the MCP transport
// registers (tool-schemas.ts), so a direct handleToolCall (tool-cli) is
// validated identically to a call that came through the SDK.
const RunDiagnosticsSchema = z.object(runDiagnosticsShape);
// Exact-device stays ubnt-airos-only by policy (no fallback path).
const RunExactDeviceDiagnosticSchema = z.object(runExactDeviceDiagnosticsShape);
const ExactIpInventorySchema = z.object(toolSchemas['lookup-device-by-exact-ip']).strict();

type ToolResult = { content: Array<{ type: 'text'; text: string }>; isError?: boolean };

interface ToolDefinition {
  name: string;
  description: string;
  inputSchema: {
    type: 'object';
    properties: Record<string, unknown>;
    required?: string[];
  };
}

export class NocMcpServer {
  private config: ServerConfig;
  private credentialStore: CredentialStore | null = null;
  private storeData: CredentialStoreData | null = null;
  private validator: CommandValidator;
  private redaction: RedactionEngine;
  private audit: AuditLogger;

  constructor(config: z.input<typeof ServerConfigSchema>) {
    this.config = ServerConfigSchema.parse(config);
    this.validator = new CommandValidator();
    this.redaction = new RedactionEngine(this.config.redaction);
    this.audit = new AuditLogger({ ...this.config.auditLog, operator: resolveOperator() });
    // Host-list env typos fail closed per call; surface them once at startup
    // (stderr only: stdout is the MCP protocol channel).
    for (const problem of hostEnvProblems()) console.error(`[config] ${problem}`);
    // New/changed SSH host keys land in the audit log (never blocks in warn mode).
    setHostKeyEventSink((ev) => this.audit.log({
      timestamp: new Date().toISOString(),
      device: ev.host,
      command: 'ssh-hostkey',
      category: 'hostkey',
      outcome: ev.blocked ? 'blocked' : ev.event === 'new' ? 'success' : 'warning',
      redactions: 0,
      durationMs: 0,
      metadata: { event: ev.event, protocol: ev.protocol, port: ev.port, fingerprint: ev.fingerprint, previous: ev.previous, mode: ev.mode, detail: ev.detail },
    }));
  }

  /** Caller-supplied context (reason / incidentId / deviceId) attached to every audit entry. */
  private callerContext(args: Record<string, unknown>): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    const reason = args.reason;
    if (typeof reason === 'string') out.callerReason = stripControl(reason).slice(0, 200);
    if (typeof args.incidentId === 'string') out.incidentId = stripControl(args.incidentId).slice(0, 64);
    if (typeof args.deviceId === 'string') out.deviceId = stripControl(args.deviceId).slice(0, 128);
    return out;
  }

  private auditWith(args: Record<string, unknown>, event: AuditEvent): void {
    this.audit.log({ ...event, metadata: { ...this.callerContext(args), ...(event.metadata ?? {}) } });
  }

  /**
   * Text that is safe to hand back to the agent / audit log: control
   * characters stripped, the credential in use and the store passphrase
   * scrubbed literally, then the normal redaction patterns, then truncated.
   */
  private errText(e: unknown, credential?: DeviceTypeCredential): string {
    let text = stripControl(e instanceof Error ? e.message : String(e));
    const secrets = [credential?.password, credential?.apiToken, credential?.passphrase, process.env[this.config.credentialStore.passphraseEnvVar]];
    for (const secret of secrets) {
      if (secret && secret.length >= 4) text = text.split(secret).join('[REDACTED]');
    }
    return this.redaction.redact(text).text.slice(0, 300);
  }

  /** Read-host policy (NOC_READ_ALLOWED_HOSTS) for every tool that opens a connection. */
  private hostBlocked(args: Record<string, unknown>, host: string, command: string, deviceType?: string): ToolResult | null {
    const check = checkReadHost(host, process.env, deviceType);
    if (check.ok) return null;
    this.auditWith(args, { timestamp: new Date().toISOString(), device: stripControl(host).slice(0, 64), command, category: 'policy', outcome: 'blocked', redactions: 0, durationMs: 0, metadata: { reason: check.error } });
    return { content: [{ type: 'text', text: `Blocked: ${check.error}` }], isError: true };
  }

  getToolDefinitions() {
    const definitions: ToolDefinition[] = [
      {
        name: 'run-diagnostics',
        description: 'Run a predefined diagnostic command on a network device. Returns structured JSON with redacted sensitive data.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            host: { type: 'string', description: 'Target device IP address' },
            deviceType: { type: 'string', description: 'Device type (mikrotik-ssh, mikrotik-rest, ubnt-uisp, ubnt-airos, customer-airos, customer-mt)' },
            command: { type: 'string', description: 'Command name (e.g., system-resource, interface-list)' },
            params: { type: 'object', description: 'Command parameters', additionalProperties: true },
          },
          required: ['host', 'deviceType', 'command'],
        },
      },
      {
        name: 'run-exact-device-diagnostics',
        description: 'Run a predefined read-only diagnostic using exactly one credential bound to the supplied stable device ID. Never falls back to type-wide credentials; intended for approved device-specific CPE reads.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            deviceId: { type: 'string', description: 'Canonical trusted inventory identifier (UISP device ID or MAC)' },
            host: { type: 'string', description: 'Target device management IP' },
            deviceType: { type: 'string', enum: ['ubnt-airos'], description: 'Exact-device diagnostic is restricted to UBNT airOS CPE reads' },
            command: { type: 'string', description: 'Existing allowlisted read-only command' },
            params: { type: 'object', description: 'Command parameters', additionalProperties: true },
          },
          required: ['deviceId', 'host', 'deviceType', 'command'],
        },
      },
      {
        name: 'list-device-types',
        description: 'List all available device types and their supported commands.',
        inputSchema: {
          type: 'object' as const,
          properties: {},
        },
      },
      {
        name: 'routeros-write',
        description: 'GATED RouterOS infrastructure write (closed op set: poe-off/poe-forced-on/poe-auto/poe-cycle/interface-enable/interface-disable). Requires NOC_WRITE_ENABLED=true, NOC_WRITE_ROUTEROS_TRANSPORT=verified, a host in write scope (private/link-local/loopback by default), explicit execute:true, reason text, and a bound exact-device mikrotik-ssh credential. execute:false returns the dry-run plan.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            op: { type: 'string', description: 'poe-off | poe-forced-on | poe-auto | poe-cycle | interface-enable | interface-disable' },
            host: { type: 'string', description: 'Target router management IP (must be allowlisted)' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match exactly one credential binding)' },
            interfaze: { type: 'string', description: 'Interface name (no shell metacharacters)' },
            reason: { type: 'string', description: 'Why this write is happening (audited)' },
            execute: { type: 'boolean', description: 'Must be true; otherwise returns the dry-run plan only' },
          },
          required: ['op', 'host', 'deviceId', 'interfaze', 'reason', 'execute'],
        },
      },
      {
        name: 'routeros-temp-access',
        description: 'GATED RouterOS temporary-access writes for factory-reset device recovery (closed op set: temp-address | temp-dhcp | temp-nat | temp-cleanup). Puts the router on the reset device subnet, serves a Wave a DHCP lease, NATs an airOS device, and cleans up — every object is tagged comment="temp reprogram-device" and cleanup removes only that tag. Same gates as routeros-write: NOC_WRITE_ENABLED=true, NOC_WRITE_ROUTEROS_TRANSPORT=verified, a host in write scope, explicit execute:true, and a bound exact-device mikrotik-ssh credential. execute:false returns the dry-run plan.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            op: { type: 'string', description: 'temp-address | temp-dhcp | temp-nat | temp-cleanup' },
            host: { type: 'string', description: 'Target router management IP (must be allowlisted)' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match exactly one credential binding)' },
            interfaze: { type: 'string', description: 'Interface facing the reset device (all ops except temp-cleanup)' },
            address: { type: 'string', description: 'temp-address: router address to add, e.g. 192.168.1.1/24 (RFC1918, /24../29)' },
            poolName: { type: 'string', description: 'temp-dhcp: pool + server name, default waveDHCP' },
            poolRange: { type: 'string', description: 'temp-dhcp: the single leased address, e.g. 192.168.1.100' },
            network: { type: 'string', description: 'temp-dhcp/temp-nat: the temp subnet, e.g. 192.168.1.0/24' },
            gateway: { type: 'string', description: 'temp-dhcp: router address handed to the radio, e.g. 192.168.1.1' },
            dstAddress: { type: 'string', description: 'temp-nat: routable mgmt address for the dstnat (optional)' },
            dstPort: { type: 'number', description: 'temp-nat: dstnat port (optional)' },
            toAddress: { type: 'string', description: 'temp-nat: factory address on the reset device (optional)' },
            toPort: { type: 'number', description: 'temp-nat: port on the reset device (optional)' },
            reason: { type: 'string', description: 'Why this write is happening (audited)' },
            execute: { type: 'boolean', description: 'Must be true; otherwise returns the dry-run plan only' },
          },
          required: ['op', 'host', 'deviceId', 'reason'],
        },
      },
      {
        name: 'temp-radio-plan',
        description: 'Read-only temp radio planner: validates a typed UBNT station-flip/frequency/reboot op and returns exact apply/verify/rollback steps with a confirm-by deadline. Never connects or executes.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            op: { type: 'string', description: 'set-mode | set-frequency | reboot' },
            incidentId: { type: 'string', description: 'Unique incident/change ID for tagging' },
            host: { type: 'string', description: 'Target radio management IP (plan only)' },
            deviceId: { type: 'string', description: 'Stable inventory device ID' },
            mode: { type: 'string', description: 'managed (station) or master (AP), for set-mode' },
            ssid: { type: 'string', description: 'Target SSID, for set-mode' },
            frequencyMhz: { type: 'number', description: 'Target frequency MHz, for set-frequency' },
            chanbw: { type: 'number', description: 'Channel width 10/20/30/40, for set-frequency' },
            previousFrequencyMhz: { type: 'number', description: 'Previous frequency (0 = AUTO misconfiguration), for set-frequency' },
            confirmWithinSeconds: { type: 'number', description: 'Safe-mode confirm deadline 60-1800s' },
          },
          required: ['op', 'incidentId', 'host', 'deviceId'],
        },
      },
      {
        name: 'temp-radio-apply',
        description: 'GATED temp radio executor (airOS SSH: anchored sed on allowlisted /tmp/system.cfg keys, re-read verify, operator-verified softrestart save, snapshot + rollback). Requires TEMP_SITE_WRITE_ENABLED=true, TEMP_SITE_AIROS_TRANSPORT=verified, host in TEMP_SITE_ALLOWED_HOSTS, explicit execute:true, and a bound exact-device credential.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            op: { type: 'string', description: 'set-mode | set-frequency | reboot' },
            incidentId: { type: 'string', description: 'Unique incident/change ID' },
            host: { type: 'string', description: 'Target radio management IP (must be allowlisted)' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match exactly one credential binding)' },
            mode: { type: 'string', description: 'managed or master, for set-mode' },
            ssid: { type: 'string', description: 'Target SSID, for set-mode' },
            frequencyMhz: { type: 'number', description: 'Target frequency MHz, for set-frequency' },
            chanbw: { type: 'number', description: 'Channel width, for set-frequency' },
            previousFrequencyMhz: { type: 'number', description: 'Previous frequency, for set-frequency' },
            confirmWithinSeconds: { type: 'number', description: 'Safe-mode confirm deadline' },
            execute: { type: 'boolean', description: 'Must be true; absence or false returns the dry-run plan only' },
          },
          required: ['op', 'incidentId', 'host', 'deviceId', 'execute'],
        },
      },
      {
        name: 'temp-radio-rollback',
        description: 'Restore the owner-only pre-apply snapshot for an incident+host (SFTP put, softrestart save). Same gates as apply; the safe-mode counterpart when a confirm deadline is missed.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Incident ID from the apply' },
            host: { type: 'string', description: 'Target radio management IP' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match the binding)' },
          },
          required: ['incidentId', 'host', 'deviceId'],
        },
      },
      {
        name: 'temp-ospf-apply',
        description: 'GATED RouterOS Temp OSPF executor (persistent SSH, preflight conflict check, tagged /30 address + OSPF cost, readback verify, auto-rollback). Requires TEMP_SITE_WRITE_ENABLED=true, TEMP_SITE_ROUTEROS_TRANSPORT=verified, host in TEMP_SITE_ALLOWED_HOSTS, explicit execute:true, and a bound exact-device mikrotik-ssh credential.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Unique incident/change ID' },
            host: { type: 'string', description: 'Target router management IP (must be allowlisted)' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match exactly one credential binding)' },
            address: { type: 'string', description: 'Usable /30 host, e.g. 10.0.28.113/30' },
            network: { type: 'string', description: 'Aligned /30 network' },
            interfaze: { type: 'string', description: 'Link-facing interface' },
            ospfArea: { type: 'string', description: 'OSPF area' },
            cost: { type: 'number', description: 'Temp OSPF cost: MUST be 1000 (same fiber drain) or 10000 (cross-drain). Any other value is rejected.', enum: [1000, 10000] },
            routerOsVersion: { type: 'string', description: 'ros6 or ros7' },
            execute: { type: 'boolean', description: 'Must be true; otherwise returns the dry-run plan' },
          },
          required: ['incidentId', 'host', 'deviceId', 'address', 'network', 'interfaze', 'execute'],
        },
      },
      {
        name: 'temp-ospf-rollback',
        description: 'Remove this incident TEMP OSPF objects by tag comment and verify removal. Same gates as apply.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Incident ID from the apply' },
            host: { type: 'string', description: 'Target router management IP' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match the binding)' },
            routerOsVersion: { type: 'string', description: 'ros6 or ros7' },
          },
          required: ['incidentId', 'host', 'deviceId'],
        },
      },
      {
        name: 'temp-mt-flip',
        description: 'GATED MikroTik wireless flip to station-bridge (minimal diff: mode+ssid only; security/frequency untouched). Requires TEMP_SITE_WRITE_ENABLED=true, TEMP_SITE_MT_TRANSPORT=verified, host in TEMP_SITE_ALLOWED_HOSTS, explicit execute:true, and a bound exact-device mikrotik-ssh credential. execute:false returns the validated plan.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Unique incident/change ID' },
            host: { type: 'string', description: 'Target radio management IP (must be allowlisted)' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match exactly one credential binding)' },
            iface: { type: 'string', description: 'Wireless interface, default wlan1' },
            ssid: { type: 'string', description: 'Target AP SSID' },
            execute: { type: 'boolean', description: 'Must be true; otherwise returns the validated plan' },
            confirmWithinSeconds: { type: 'number', description: 'Safe-mode confirm deadline' },
          },
          required: ['incidentId', 'host', 'deviceId', 'ssid', 'execute'],
        },
      },
      {
        name: 'temp-mt-rollback',
        description: 'Restore recorded previous mode/ssid on a MikroTik wireless interface. Same gates as flip.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Incident ID (audit only)' },
            host: { type: 'string', description: 'Target radio management IP' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match the binding)' },
            iface: { type: 'string', description: 'Wireless interface, default wlan1' },
            mode: { type: 'string', description: 'Mode to restore (e.g. ap-bridge)' },
            ssid: { type: 'string', description: 'SSID to restore' },
          },
          required: ['incidentId', 'host', 'deviceId', 'mode', 'ssid'],
        },
      },
      {
        name: 'temp-ap-wds',
        description: 'GATED AP-side WDS toggle for station-bridge data path (dynamic/disabled). Same gates as flip; station reassociates automatically.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Unique incident/change ID' },
            host: { type: 'string', description: 'Target AP management IP (must be allowlisted)' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match exactly one credential binding)' },
            iface: { type: 'string', description: 'Wireless interface, default wlan1' },
            wdsMode: { type: 'string', description: 'dynamic or disabled' },
            execute: { type: 'boolean', description: 'Must be true; otherwise returns the validated plan' },
          },
          required: ['incidentId', 'host', 'deviceId', 'execute'],
        },
      },
      {
        name: 'temp-mt-bounce',
        description: 'GATED station re-associate bounce (disable/enable wlan, station-bridge only). Forces fresh association to pick up AP-side changes like WDS. Same gates as flip.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Unique incident/change ID' },
            host: { type: 'string', description: 'Target station management IP (must be allowlisted)' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match exactly one credential binding)' },
            iface: { type: 'string', description: 'Wireless interface, default wlan1' },
            execute: { type: 'boolean', description: 'Must be true; otherwise returns the validated plan' },
          },
          required: ['incidentId', 'host', 'deviceId', 'execute'],
        },
      },
      {
        name: 'temp-mt-alias',
        description: 'GATED same-subnet temp address on a station radio bridge (e.g. island-subnet alias for forced-L2 ping/SSH diagnosis). Tagged for incident cleanup; remove with temp-mt-alias-remove. Same gates as flip.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Unique incident/change ID (address is tagged for removal)' },
            host: { type: 'string', description: 'Target radio management IP (must be allowlisted)' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match exactly one credential binding)' },
            address: { type: 'string', description: 'Temp IPv4/CIDR, e.g. 10.0.20.60/24' },
            interfaze: { type: 'string', description: 'Interface for the alias, default bridge1' },
            execute: { type: 'boolean', description: 'Must be true; otherwise returns the validated plan' },
          },
          required: ['incidentId', 'host', 'deviceId', 'address', 'execute'],
        },
      },
      {
        name: 'temp-mt-alias-remove',
        description: 'Remove this incident\'s temp alias address by tag. Same gates as flip.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Incident ID whose tag to remove' },
            host: { type: 'string', description: 'Target radio management IP' },
            deviceId: { type: 'string', description: 'Stable inventory device ID (must match the binding)' },
          },
          required: ['incidentId', 'host', 'deviceId'],
        },
      },
      {
        name: 'temp-ospf-plan',
        description: 'Read-only Temp OSPF planner: validates a /30 host + interface and returns tagged RouterOS apply/rollback commands plus preflight checklist. Never connects to devices or executes writes.',
        inputSchema: {
          type: 'object' as const,
          properties: {
            incidentId: { type: 'string', description: 'Unique incident/change ID for tagging' },
            host: { type: 'string', description: 'Target router management IP (plan only, no connection)' },
            interfaze: { type: 'string', description: 'Link-facing interface name' },
            address: { type: 'string', description: 'Usable /30 host address, e.g. 10.0.28.113/30' },
            network: { type: 'string', description: 'Aligned /30 network, e.g. 10.0.28.112/30' },
            ospfType: { type: 'string', description: 'REQUIRED, no default: ptp (facing interface is a standalone ether) or broadcast (facing interface is a bridge or bridge-port member) — determine from live /interface bridge port print first', enum: ['ptp', 'broadcast'] },
            ospfArea: { type: 'string', description: 'OSPF area, default backbone' },
            cost: { type: 'number', description: 'Temp OSPF cost: MUST be 1000 (same fiber drain) or 10000 (cross-drain). Any other value is rejected; omit for the 1000 default.', enum: [1000, 10000] },
            routerOsVersion: { type: 'string', description: 'ros6 or ros7' },
          },
          required: ['incidentId', 'host', 'interfaze', 'address', 'network', 'ospfType'],
        },
      },
    ];
    if (this.config.inventory.enabled) {
      definitions.push({
        name: 'lookup-device-by-exact-ip',
        description: 'Read-only exact IPv4 lookup in LibreNMS inventory. Returns source, retrieval time, poll freshness, and identity fields only; does not prove site truth, current reachability, topology, or radio role.',
        inputSchema: {
          type: 'object' as const,
          properties: { ip: { type: 'string', description: 'Exact IPv4 management address to look up' } },
          required: ['ip'],
        },
      });
    }
    definitions.push({
      name: 'snmp-config',
      description: 'GATED SNMP provisioning for devices that are up but unmonitored (closed op set: snmp-enable | snmp-verify; vendors: mikrotik RouterOS /snmp, airos /tmp/system.cfg, wave WaveOS ucfg services.snmp). snmp-enable applies the operator-standard config (read-only community with security=none, NOC_SNMP_COMMUNITY_ADDRESSES as addresses= when set, trap-community, and a stale /snmp src-address reset to ::) and needs NOC_WRITE_ENABLED=true, NOC_WRITE_SNMP_TRANSPORT=verified, a host in write scope (private/link-local/loopback by default), execute:true, and a credential (exact-device binding, or the device-type pool only when NOC_WRITE_SNMP_POOL_CREDENTIALS=true). The community comes only from NOC_SNMP_COMMUNITY and is redacted everywhere. snmp-verify is read-only. execute:false returns the dry-run plan.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          op: { type: 'string', description: 'snmp-enable | snmp-verify', enum: ['snmp-enable', 'snmp-verify'] },
          vendor: { type: 'string', description: 'mikrotik (RouterOS /snmp) | aios (airOS system.cfg snmp.* keys) | wave (WaveOS ucfg services.snmp)', enum: ['mikrotik', 'airos', 'wave'] },
          host: { type: 'string', description: 'Device management IPv4' },
          deviceId: { type: 'string', description: 'Inventory/device ID for the credential binding' },
          reason: { type: 'string', description: 'Why this device needs SNMP now (audit trail)' },
          execute: { type: 'boolean', description: 'false/absent = dry-run plan; true = apply' },
        },
        required: ['op', 'vendor', 'host', 'deviceId', 'reason'],
      },
    });
    definitions.push({
      name: 'librenms-add-device',
      description: 'GATED LibreNMS device add for a device that already answers SNMP. Pre-flight is read-only: exact-IP lookup (refuses when the address is already monitored) and one SNMPv2c GET for sysName using NOC_SNMP_COMMUNITY — silence means no add, because LibreNMS would only create a device that never polls. The add request never sets force_add, so a sysName collision is refused by LibreNMS and reported verbatim rather than overridden. Needs NOC_WRITE_ENABLED=true, inventory.enabled with a token, a host in write scope, and execute:true. execute:false returns the plan for a device already proven to answer SNMP.',
      inputSchema: {
        type: 'object' as const,
        properties: {
          host: { type: 'string', description: 'Device management IPv4 to add' },
          reason: { type: 'string', description: 'Why this device belongs in monitoring now (audit trail)' },
          execute: { type: 'boolean', description: 'false/absent = dry-run plan; true = add' },
        },
        required: ['host', 'reason'],
      },
    });
    return definitions;
  }

  private async routerOsWrite(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const parsed = RouterOsWriteSchema.safeParse(args);
    const device = String((args as Record<string, unknown>).host ?? 'unknown');
    const baseAudit = { timestamp: new Date().toISOString(), device, command: 'routeros-write', category: 'execution', redactions: 0, durationMs: 0 };
    if (!parsed.success) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'schema validation failed' } });
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map((i: { message: string }) => i.message).join(', ')}` }], isError: true };
    }
    const input = parsed.data;
    if (input.execute !== true) {
      const plan = planRouterOsWrite(input);
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'execute flag not set; plan only' } });
      return { content: [{ type: 'text', text: JSON.stringify({ ...plan, note: 'execute was not true: dry-run plan only, nothing written.' }, null, 2) }], isError: !plan.valid };
    }
    const writePlan = planRouterOsWrite(input);
    if (!writePlan.valid) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: writePlan.error } });
      return { content: [{ type: 'text', text: `Blocked: ${writePlan.error}` }], isError: true };
    }
    const gates = checkInfraWriteGates(input.host);
    if (!gates.ok) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: gates.error } });
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', input.deviceId, input.host);
    if (!credential) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'no unique exact-device mikrotik-ssh binding' } });
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host; type-wide credentials are never used for writes.' }], isError: true };
    }
    const startedAt = Date.now();
    const hostPolicy = this.hostBlocked(args, input.host, 'routeros-write');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(input.host, credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const result = await executeRouterOsWrite((cmd: string) => client.runRaw(cmd), input, {
        // poe-cycle restores power in a finally; if the session died during the
        // dwell, retry the restore once on a fresh connection.
        reconnect: async () => {
          await client.disconnect().catch(() => { /* best effort */ });
          await client.connect();
          return (cmd: string) => client.runRaw(cmd);
        },
      });
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { ...baseAudit, outcome: 'success', durationMs: Date.now() - startedAt, metadata: { op: input.op, interfaze: input.interfaze, reason: input.reason } });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      this.auditWith(args, { ...baseAudit, outcome: 'error', durationMs: Date.now() - startedAt, metadata: { op: input.op, reason: this.errText(e, credential) } });
      return { content: [{ type: 'text', text: `routeros-write failed (${this.errText(e, credential)}). Inspect router state before retrying.` }], isError: true };
    }
  }

  /**
   * Gated temp-access writes for factory-reset recovery. Same gate order as
   * routeros-write: schema -> plan -> execute flag -> gates -> exact binding ->
   * host policy -> connect. The exact-device binding is what keeps this from
   * reaching any router the operator has not explicitly provisioned.
   */
  private async routerOsTempAccess(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const parsed = RouterOsTempAccessSchema.safeParse(args);
    const device = String((args as Record<string, unknown>).host ?? 'unknown');
    const baseAudit = { timestamp: new Date().toISOString(), device, command: 'routeros-temp-access', category: 'execution', redactions: 0, durationMs: 0 };
    if (!parsed.success) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'schema validation failed' } });
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map((i: { message: string }) => i.message).join(', ')}` }], isError: true };
    }
    const input: RouterOsTempAccess = parsed.data;
    if (input.execute !== true) {
      const plan = planRouterOsTempAccess(input);
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'execute flag not set; plan only' } });
      return { content: [{ type: 'text', text: JSON.stringify({ ...plan, note: 'execute was not true: dry-run plan only, nothing written.' }, null, 2) }], isError: !plan.valid };
    }
    const writePlan = planRouterOsTempAccess(input);
    if (!writePlan.valid) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: writePlan.error } });
      return { content: [{ type: 'text', text: `Blocked: ${writePlan.error}` }], isError: true };
    }
    const gates = checkTempAccessGates(input.host);
    if (!gates.ok) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: gates.error } });
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', input.deviceId, input.host);
    if (!credential) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'no unique exact-device mikrotik-ssh binding' } });
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host; type-wide credentials are never used for writes.' }], isError: true };
    }
    const startedAt = Date.now();
    const hostPolicy = this.hostBlocked(args, input.host, 'routeros-temp-access');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(input.host, credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const result = await executeRouterOsTempAccess((cmd: string) => client.runRaw(cmd), input);
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { ...baseAudit, outcome: 'success', durationMs: Date.now() - startedAt, metadata: { op: input.op, interfaze: input.interfaze, applied: result.output.length, skipped: result.skipped.length, reason: input.reason } });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      this.auditWith(args, { ...baseAudit, outcome: 'error', durationMs: Date.now() - startedAt, metadata: { op: input.op, reason: this.errText(e, credential) } });
      return { content: [{ type: 'text', text: `routeros-temp-access failed (${this.errText(e, credential)}). Inspect router state before retrying.` }], isError: true };
    }
  }

  /**
   * Gated SNMP provisioning — enable the read-only SNMP service on a device that
   * is up but unmonitored, before it is added to LibreNMS. Gate order matches the
   * other executors: schema -> settings from env -> plan -> execute flag -> write
   * gates (enable only) -> credential -> host policy -> connect.
   *
   * The one deliberate difference: credentials may come from the device-type
   * pool the read path already uses, because the devices this exists for have no
   * per-device binding yet. That needs NOC_WRITE_SNMP_POOL_CREDENTIALS=true, is
   * tried in priority order with a bounded count, and still never leaves the
   * store — audit records the credential NAME, never the secret. The community is
   * read from the environment only and redacted from every result and audit line.
   */
  private async snmpConfig(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const device = String((args as Record<string, unknown>).host ?? 'unknown');
    const baseAudit = { timestamp: new Date().toISOString(), device, command: 'snmp-config', category: 'execution', redactions: 0, durationMs: 0 };
    const parsed = SnmpConfigSchema.safeParse(args);
    if (!parsed.success) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'schema validation failed' } });
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map((i: { message: string }) => i.message).join(', ')}` }], isError: true };
    }
    const input: SnmpConfig = parsed.data;

    let settings: SnmpSettings | undefined;
    if (input.op === 'snmp-enable') {
      const settingsResult = readSnmpSettings();
      if (!settingsResult.ok) {
        this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: settingsResult.error } });
        return { content: [{ type: 'text', text: `Blocked: ${settingsResult.error}` }], isError: true };
      }
      settings = settingsResult.settings;
    }
    const plan = planSnmpConfig(input, settings);
    if (!plan.valid) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: plan.error } });
      return { content: [{ type: 'text', text: `Blocked: ${plan.error}` }], isError: true };
    }
    if (input.execute !== true) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'execute flag not set; plan only' } });
      return { content: [{ type: 'text', text: JSON.stringify({ ...redactPlan(plan, settings?.community), note: 'execute was not true: dry-run plan only, nothing written.' }, null, 2) }] };
    }
    if (input.op === 'snmp-enable') {
      const gates = checkSnmpWriteGates(input.host);
      if (!gates.ok) {
        this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: gates.error } });
        return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
      }
    }

    const resolver = new CredentialResolver(await this.getStoreData());
    const deviceTypes = VENDOR_DEVICE_TYPES[input.vendor];
    const candidates: Array<{ credential: DeviceTypeCredential; source: string }> = [];
    for (const dt of deviceTypes) {
      const exact = resolver.resolveExactDevice(dt, input.deviceId, input.host);
      if (exact) candidates.push({ credential: exact, source: `exact-device binding (${dt})` });
    }
    if (!candidates.length && poolCredentialsEnabled()) {
      // Round-robin the pools so a small try budget still reaches the CPE pool.
      const perPool = deviceTypes.map((dt) =>
        resolver.resolveByType(dt).map((credential) => ({
          credential,
          source: `device-type pool ${dt} (NOC_WRITE_SNMP_POOL_CREDENTIALS=true)`,
        })),
      );
      candidates.push(...interleavePoolCandidates(perPool).slice(0, poolCredentialTries()));
    }
    if (!candidates.length) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'no credential for this device' } });
      return { content: [{ type: 'text', text: 'Blocked: no exact-device credential is bound to this ID+host and NOC_WRITE_SNMP_POOL_CREDENTIALS is not true. Provision a binding, or opt the device-type pool in and re-run.' }], isError: true };
    }
    const hostPolicy = this.hostBlocked(args, input.host, 'snmp-config');
    if (hostPolicy) return hostPolicy;

    const startedAt = Date.now();
    const failures: string[] = [];
    for (const candidate of candidates) {
      try {
        const result = await this.runSnmpOnDevice(input, plan, settings, candidate.credential);
        this.auditWith(args, {
          ...baseAudit,
          outcome: 'success',
          durationMs: Date.now() - startedAt,
          metadata: { op: input.op, vendor: input.vendor, credentialSource: candidate.source, credentialName: candidate.credential.name, attempts: failures.length + 1 },
        });
        const payload = {
          ...result,
          credentialSource: candidate.source,
          credentialName: candidate.credential.name,
          attempts: failures.length + 1,
          plan: redactPlan(plan, settings?.community),
          note: input.op === 'snmp-enable'
            ? (input.vendor === 'wave'
              ? `WaveOS services.snmp.community + contact (${settings?.contact ?? 'unset, left as it is'}) + enabled verified on the radio${result.locationSet ? `, location ${JSON.stringify(result.locationSet)} from its own GPS fix` : '; no GPS fix supplied, existing location untouched'}. librenms-add-device plus a successful poll is the only real proof SNMP answers.`
              : input.vendor === 'airos'
                ? `snmp.status + snmp.community verified in /tmp/system.cfg, sysContact ${JSON.stringify(settings?.contact ?? 'unset, left as it is')}${plan.location ? ` and location ${JSON.stringify(plan.location)}` : ''} where the build carries those lines. librenms-add-device plus a successful poll is the only real proof SNMP answers.`
                : `Standard RouterOS config verified (community, addresses=, trap-community, src-address, sysContact ${JSON.stringify(settings?.contact ?? 'unset, left as it is')}${plan.location ? `, location ${JSON.stringify(plan.location)}` : ''}). librenms-add-device plus a successful poll is the only real proof SNMP answers.`)
            : 'Read-only settings snapshot; nothing was written.',
        };
        // Two redactions, both needed: by shape first (any community name the
        // read-back printed, including ours), then ours by value as a backstop.
        // Shape first matters: redacting the value first leaves a placeholder with
        // spaces, which the shape pass would then cut in half.
        const rendered = redactCommunity(
          redactVerifyOutput(JSON.stringify(payload, null, 2), input.vendor),
          settings?.community,
        );
        return { content: [{ type: 'text', text: rendered }] };
      } catch (e) {
        failures.push(this.errText(e, candidate.credential));
      }
    }
    this.auditWith(args, { ...baseAudit, outcome: 'error', durationMs: Date.now() - startedAt, metadata: { op: input.op, vendor: input.vendor, attempts: failures.length, reason: failures.join(' | ').slice(0, 300) } });
    const detail = redactCommunity(failures.slice(0, 3).join(' | '), settings?.community);
    return { content: [{ type: 'text', text: `snmp-config failed on every credential tried (${failures.length}). Inspect the device before retrying. Detail: ${detail}` }], isError: true };
  }

  /** Vendor transport for one credential. Throws so the caller can try the next one. */
  private async runSnmpOnDevice(input: SnmpConfig, plan: SnmpPlan, settings: SnmpSettings | undefined, credential: DeviceTypeCredential): Promise<Record<string, unknown>> {
    const timeout = this.config.limits.commandTimeoutMs;
    if (input.vendor === 'mikrotik') {
      const client = createDeviceClient(input.host, credential, 'mikrotik-ssh', timeout) as MikroTikClient;
      try {
        await client.connect();
        if (input.op === 'snmp-verify') {
          return { applied: false, verify: redactVerifyOutput(await client.runRaw(plan.verifyCmd ?? '/snmp print'), 'mikrotik') };
        }
        if (!settings) throw new Error('snmp-enable needs NOC_SNMP_COMMUNITY');
        return { ...(await executeMikrotikSnmp((cmd: string) => client.runRaw(cmd), settings, plan)) };
      } finally {
        await client.disconnect().catch(() => { /* best effort */ });
      }
    }

    if (input.vendor === 'wave') {
      // WaveOS: `ucfg edit` + `ucfg commit` over the ubnt-wave credential pool.
      const runner = new Ssh2Runner();
      const readCmd = plan.waveReadCmd ?? 'ucfg show';
      if (input.op === 'snmp-verify') {
        // Never the raw ucfg dump: it also contains services.uisp.key and the
        // wireless passphrase. Project services.snmp only.
        return { applied: false, verify: waveSnmpOnlyView(await runner.exec(input.host, credential, readCmd, timeout)) };
      }
      if (!settings) throw new Error('snmp-enable needs NOC_SNMP_COMMUNITY');
      const result = await executeWaveSnmp((cmd: string) => runner.exec(input.host, credential, cmd, timeout), settings, plan);
      if (!result.committed) {
        // Live config is proven; persistence is not. Say so rather than claim success.
        return {
          ...result,
          warning: result.commitReported
            ? 'ucfg commit returned but the radio never answered the post-commit re-read; persistence is unproven — run snmp-verify to confirm.'
            : 'ucfg commit dropped the session and the post-commit re-read failed; the live config is correct but persistence is unproven — run snmp-verify to confirm.',
        };
      }
      return { ...result };
    }

    const runner = new Ssh2Runner();
    if (input.op === 'snmp-verify') {
      return { applied: false, verify: redactVerifyOutput(await runner.exec(input.host, credential, plan.verifyCmd ?? "grep -E '^snmp\\.' /tmp/system.cfg", timeout), 'airos') };
    }
    if (!settings || !plan.edits?.length) throw new Error('snmp-enable needs a plan with airOS edits');
    // sed cannot create a key: refuse before touching the radio rather than let the
    // re-read fail and roll a half-applied config back.
    const present = await runner.exec(input.host, credential, plan.verifyCmd ?? "grep -E '^snmp\\.' /tmp/system.cfg", timeout);
    const missing = plan.edits.filter((e) => !present.includes(`${e.key}=`)).map((e) => e.key);
    if (missing.length) {
      throw new Error(`airOS build has no ${missing.join(', ')} line in /tmp/system.cfg; switch SNMP on once from the web UI, then re-run (nothing was written)`);
    }
    const edits = [...plan.edits, ...(plan.optionalEdits ?? []).filter((e) => present.includes(`${e.key}=`))];
    const result = await applyCfgEdits(runner, input.host, credential, edits, async () => {
      try {
        const check = await runner.exec(input.host, credential, plan.verifyCmd ?? '', 20000);
        // Redact before returning: this detail is the post-apply grep, and the
        // community is on a line in it.
        return { ok: edits.every((e) => check.includes(`${e.key}=${e.value}`)), detail: redactVerifyOutput(check.trim().slice(0, 300) || 'empty readback', 'airos') };
      } catch (e) {
        return { ok: false, detail: this.errText(e, credential) };
      }
    }, timeout);
    return { ...result };
  }

  async handleToolCall(name: string, rawArgs: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    if (!Object.prototype.hasOwnProperty.call(toolSchemas, name)) {
      return { content: [{ type: 'text', text: `Unknown tool: ${stripControl(name).slice(0, 64)}` }], isError: true };
    }
    // One validation choke point for BOTH transports: the MCP SDK has already
    // parsed with these shapes, and tool-cli (which calls this directly) now
    // goes through exactly the same schemas.
    const parsedArgs = parseToolArgs(name, rawArgs);
    if (!parsedArgs.ok) {
      const host = typeof rawArgs.host === 'string' ? stripControl(rawArgs.host).slice(0, 64) : 'unknown';
      this.auditWith(rawArgs, { timestamp: new Date().toISOString(), device: host, command: name, category: 'validation', outcome: 'blocked', redactions: 0, durationMs: 0, metadata: { reason: 'schema validation failed', errors: parsedArgs.error.slice(0, 400) } });
      return { content: [{ type: 'text', text: `Validation error: ${parsedArgs.error}` }], isError: true };
    }
    const args = parsedArgs.data;
    try {
      switch (name) {
        case 'run-diagnostics':
          return await this.runDiagnostics(args);
        case 'run-exact-device-diagnostics':
          return await this.runExactDeviceDiagnostics(args);
        case 'list-device-types':
          return await this.listDevices();
        case 'temp-ospf-plan':
          return await this.tempOspfPlan(args);
        case 'temp-radio-plan':
          return await this.tempRadioPlan(args);
        case 'temp-radio-apply':
          return await this.tempRadioApply(args);
        case 'temp-radio-rollback':
          return await this.tempRadioRollback(args);
        case 'temp-ospf-apply':
          return await this.tempOspfApply(args);
        case 'temp-ospf-rollback':
          return await this.tempOspfRollback(args);
        case 'temp-mt-flip':
          return await this.tempMtFlip(args);
        case 'temp-mt-rollback':
          return await this.tempMtRollback(args);
        case 'temp-mt-alias':
          return this.tempMtAlias(args);
        case 'temp-mt-alias-remove':
          return this.tempMtAliasRemove(args);
        case 'temp-mt-bounce':
          return await this.tempMtBounce(args);
        case 'temp-ap-wds':
          return await this.tempApWds(args);
        case 'routeros-write':
          return await this.routerOsWrite(args);
        case 'routeros-temp-access':
          return await this.routerOsTempAccess(args);
        case 'snmp-config':
          return await this.snmpConfig(args);
        case 'librenms-add-device':
          return await this.librenmsAddDevice(args);
        case 'lookup-device-by-exact-ip':
          return await this.lookupDeviceByExactIp(args);
        default:
          return { content: [{ type: 'text', text: `Unknown tool: ${name}` }], isError: true };
      }
    } catch (error) {
      return {
        content: [{ type: 'text', text: `Error: ${this.errText(error)}` }],
        isError: true,
      };
    }
  }

  private async lookupDeviceByExactIp(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const parsed = ExactIpInventorySchema.safeParse(args);
    if (!parsed.success) {
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map(issue => issue.message).join(', ')}` }], isError: true };
    }

    if (!this.config.inventory.enabled) {
      this.auditWith(args, { timestamp: new Date().toISOString(), device: parsed.data.ip, command: 'lookup-device-by-exact-ip', category: 'inventory', outcome: 'blocked', redactions: 0, durationMs: 0, metadata: { reason: 'inventory lookup disabled by configuration' } });
      return { content: [{ type: 'text', text: 'Blocked: exact-IP inventory lookup is disabled by configuration.' }], isError: true };
    }

    const startedAt = Date.now();
    const token = process.env[this.config.inventory.libreNmsTokenEnvVar] ?? '';
    try {
      const result = await lookupLibreNmsDeviceByExactIp(parsed.data.ip, {
        baseUrl: this.config.inventory.libreNmsBaseUrl,
        token,
        timeoutMs: this.config.inventory.lookupTimeoutMs,
        staleAfterSeconds: this.config.inventory.staleAfterSeconds,
      });
      this.auditWith(args, { timestamp: result.retrievedAt, device: parsed.data.ip, command: 'lookup-device-by-exact-ip', category: 'inventory', outcome: result.status === 'matched' || result.status === 'not_found' ? 'success' : 'error', redactions: 0, durationMs: Date.now() - startedAt, metadata: { source: 'LibreNMS', matchStatus: result.status, pollFreshness: result.device?.pollFreshness ?? 'not-applicable' } });
      const output = JSON.stringify(result, null, 2);
      return { content: [{ type: 'text', text: output.length > this.config.limits.maxOutputBytes ? `${output.substring(0, this.config.limits.maxOutputBytes)}[truncated]` : output }] };
    } catch (error) {
      this.auditWith(args, { timestamp: new Date().toISOString(), device: parsed.data.ip, command: 'lookup-device-by-exact-ip', category: 'inventory', outcome: 'error', redactions: 0, durationMs: Date.now() - startedAt, metadata: { reason: this.errText(error) } });
      // Surface the cause: a bare "lookup failed" is what hid a plain 404
      // ("device does not exist") behind what looked like a broken integration.
      return { content: [{ type: 'text', text: `LibreNMS exact-IP lookup failed: ${this.errText(error)}` }], isError: true };
    }
  }

  /**
   * Add one device to LibreNMS — only after it proves it answers SNMP.
   *
   * Order is deliberate: the exact-IP lookup runs first (an address that is
   * already monitored must never be re-added), then one SNMPv2c GET for sysName
   * with the monitoring community. Silence ends the call with no request sent:
   * LibreNMS accepts nearly anything and then creates a device that never polls,
   * which is the failure mode this pre-flight exists to prevent.
   *
   * `execute:false` returns the plan once SNMP is proven, so the operator can see
   * exactly which device would be added before anything is written.
   */
  private async librenmsAddDevice(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const device = String((args as Record<string, unknown>).host ?? 'unknown');
    const baseAudit = { timestamp: new Date().toISOString(), device, command: 'librenms-add-device', category: 'inventory', redactions: 0, durationMs: 0 };
    const parsed = LibrenmsAddSchema.safeParse(args);
    if (!parsed.success) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'schema validation failed' } });
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map((i: { message: string }) => i.message).join(', ')}` }], isError: true };
    }
    const input = parsed.data;
    if (!this.config.inventory.enabled) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'inventory disabled by configuration' } });
      return { content: [{ type: 'text', text: 'Blocked: LibreNMS inventory is disabled by configuration.' }], isError: true };
    }
    const token = process.env[this.config.inventory.libreNmsTokenEnvVar] ?? '';
    if (!token.trim()) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'no LibreNMS API token' } });
      return { content: [{ type: 'text', text: `Blocked: ${this.config.inventory.libreNmsTokenEnvVar} is not set.` }], isError: true };
    }
    const settings = readSnmpSettings();
    if (!settings.ok) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'no SNMP community to probe with' } });
      return { content: [{ type: 'text', text: `Blocked: ${settings.error} The community is needed to prove the device answers SNMP before adding it.` }], isError: true };
    }

    const startedAt = Date.now();
    const lookupOptions = {
      baseUrl: this.config.inventory.libreNmsBaseUrl,
      token,
      timeoutMs: this.config.inventory.lookupTimeoutMs,
      staleAfterSeconds: this.config.inventory.staleAfterSeconds,
    };
    try {
      const existing = await lookupLibreNmsDeviceByExactIp(input.host, lookupOptions);
      if (existing.status === 'matched') {
        this.auditWith(args, { ...baseAudit, outcome: 'blocked', durationMs: Date.now() - startedAt, metadata: { reason: 'address already monitored', deviceId: existing.device?.deviceId } });
        return { content: [{ type: 'text', text: `Already monitored: ${input.host} is LibreNMS device_id ${existing.device?.deviceId ?? 'unknown'} (${existing.device?.hostname ?? 'no hostname'}). Nothing added.` }], isError: true };
      }
      if (existing.status === 'ambiguous' || existing.status === 'identity_unverified') {
        this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: `lookup was ${existing.status}` } });
        return { content: [{ type: 'text', text: `Not added: the exact-IP lookup returned ${existing.status} for ${input.host}. ${existing.caution} A human has to resolve which record is this device.` }], isError: true };
      }

      const probe = await snmpGetSysName(input.host, settings.settings.community, {
        timeoutMs: Math.min(5000, Math.max(2000, this.config.inventory.lookupTimeoutMs)),
      });
      const snmp = { answered: probe.answered, sysName: probe.sysName ?? null, error: probe.error ?? null };
      if (!probe.ok) {
        this.auditWith(args, { ...baseAudit, outcome: 'blocked', durationMs: Date.now() - startedAt, metadata: { reason: 'device did not answer SNMP' } });
        const why = redactCommunity(probe.error ?? 'no response', settings.settings.community);
        return { content: [{ type: 'text', text: `Not added: ${input.host} did not answer SNMPv2c with the monitoring community (${why}). Enable SNMP first with snmp-config (vendor mikrotik|airos|wave), then retry — LibreNMS would otherwise create a device that never polls.` }], isError: true };
      }

      if (input.execute !== true) {
        this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'execute flag not set; plan only' } });
        return { content: [{ type: 'text', text: JSON.stringify({
          wouldAdd: true,
          host: input.host,
          snmp,
          request: librenmsAddRequestPreview(input.host),
          note: 'execute was not true: dry-run plan only, nothing added. The device answered SNMP with the monitoring community, which is the precondition for adding it.',
        }, null, 2) }] };
      }
      if (process.env.NOC_WRITE_ENABLED !== 'true') {
        this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'writes disabled' } });
        return { content: [{ type: 'text', text: 'Blocked: Writes disabled; require NOC_WRITE_ENABLED=true (kill switch, default off)' }], isError: true };
      }
      const hostPolicy = checkWriteHost(input.host);
      if (!hostPolicy.ok) {
        this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: hostPolicy.error } });
        return { content: [{ type: 'text', text: `Blocked: ${hostPolicy.error}` }], isError: true };
      }

      const result = await postLibrenmsDevice(input.host, settings.settings.community, {
        baseUrl: this.config.inventory.libreNmsBaseUrl,
        token,
        timeoutMs: this.config.inventory.lookupTimeoutMs,
      });
      if (result.status !== 'added') {
        this.auditWith(args, { ...baseAudit, outcome: 'error', durationMs: Date.now() - startedAt, metadata: { reason: result.message.slice(0, 200) } });
        return { content: [{ type: 'text', text: `LibreNMS refused to add ${input.host}: ${redactCommunity(result.message, settings.settings.community)}. force_add was not used: a sysName collision or a discovery failure needs a human decision, not an override.` }], isError: true };
      }

      // Confirm what LibreNMS actually recorded and whether the first poll landed.
      const confirmed = await lookupLibreNmsDeviceByExactIp(input.host, lookupOptions).catch(() => null);
      this.auditWith(args, { ...baseAudit, outcome: 'success', durationMs: Date.now() - startedAt, metadata: { deviceId: result.deviceId, sysName: snmp.sysName, reason: input.reason } });
      const payload = {
        added: true,
        host: input.host,
        deviceId: result.deviceId,
        message: result.message,
        snmp,
        confirmed: confirmed
          ? {
            lookup: confirmed.status,
            hostname: confirmed.device?.hostname ?? null,
            sysName: confirmed.device?.sysName ?? null,
            status: confirmed.device?.libreNmsReportedStatus ?? null,
            pollFreshness: confirmed.device?.pollFreshness ?? null,
            lastPolled: confirmed.device?.lastPolled ?? null,
          }
          : null,
        note: 'LibreNMS accepted the device. The first successful poll is the end-to-end proof of SNMP; re-run lookup-device-by-exact-ip after a few minutes and expect pollFreshness "fresh".',
      };
      return { content: [{ type: 'text', text: redactCommunity(JSON.stringify(payload, null, 2), settings.settings.community) }] };
    } catch (error) {
      this.auditWith(args, { ...baseAudit, outcome: 'error', durationMs: Date.now() - startedAt, metadata: { reason: this.errText(error) } });
      return { content: [{ type: 'text', text: `librenms-add-device failed: ${this.errText(error)}` }], isError: true };
    }
  }

  private async runExactDeviceDiagnostics(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const parsed = RunExactDeviceDiagnosticSchema.safeParse(args);
    if (!parsed.success) {
      return {
        content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map(i => i.message).join(', ')}` }],
        isError: true,
      };
    }

    const { deviceId, host, deviceType, command, params } = parsed.data;
    if (deviceType !== 'ubnt-airos') {
      this.auditWith(args, { timestamp: new Date().toISOString(), device: deviceId, command, category: 'validation', outcome: 'blocked', redactions: 0, durationMs: 0, metadata: { reason: 'exact-device diagnostics are restricted to ubnt-airos CPE reads' } });
      return { content: [{ type: 'text', text: 'Blocked: exact-device diagnostics are currently limited to UBNT airOS CPEs.' }], isError: true };
    }
    const validation = this.validator.validate(deviceType, command, params);
    if (!validation.valid) {
      this.auditWith(args, { timestamp: new Date().toISOString(), device: deviceId, command, category: 'validation', outcome: 'blocked', redactions: 0, durationMs: 0, metadata: { reason: validation.error } });
      return { content: [{ type: 'text', text: `Validation error: ${validation.error}` }], isError: true };
    }

    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice(deviceType, deviceId, host);
    if (!credential) {
      this.auditWith(args, { timestamp: new Date().toISOString(), device: deviceId, command, category: 'auth', outcome: 'blocked', redactions: 0, durationMs: 0, metadata: { reason: 'no unique exact-device credential binding' } });
      return { content: [{ type: 'text', text: 'Blocked: no unique credential is bound to this exact device ID; no fallback credentials were attempted.' }], isError: true };
    }

    const hostPolicy = this.hostBlocked(args, host, command);
    if (hostPolicy) return hostPolicy;

    const client = createDeviceClient(host, credential, deviceType, this.config.limits.commandTimeoutMs);
    const startedAt = Date.now();
    try {
      await client.connect();
      const result = await client.execute(validation.command!, params);
      await client.disconnect();
      const redacted = this.redaction.redact(JSON.stringify(result.result));
      this.auditWith(args, { timestamp: result.timestamp, device: deviceId, command, category: result.category, outcome: result.status === 'success' ? 'success' : 'error', redactions: redacted.count, durationMs: result.durationMs, metadata: { credentialMode: 'exact-device' } });
      let outputText = JSON.stringify({ ...result, deviceId, result: JSON.parse(redacted.text), redactions: redacted.count }, null, 2);
      if (outputText.length > this.config.limits.maxOutputBytes) outputText = outputText.substring(0, this.config.limits.maxOutputBytes) + `[truncated - output exceeded ${this.config.limits.maxOutputBytes} bytes]`;
      return { content: [{ type: 'text', text: outputText }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* best effort cleanup; do not retry credentials */ }
      this.auditWith(args, { timestamp: new Date().toISOString(), device: deviceId, command, category: 'auth', outcome: 'auth_failed', redactions: 0, durationMs: Date.now() - startedAt, metadata: { credentialMode: 'exact-device', attempts: 1 } });
      return { content: [{ type: 'text', text: 'Exact-device diagnostic failed; one bound credential was attempted and no fallback was tried. Reason: ' + (e instanceof Error ? e.message : 'unknown') }], isError: true };
    }
  }

  private async runDiagnostics(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const parsed = RunDiagnosticsSchema.safeParse(args);
    if (!parsed.success) {
      this.auditWith(args, {
        timestamp: new Date().toISOString(),
        device: (args.host as string) ?? 'unknown',
        command: (args.command as string) ?? 'unknown',
        category: 'validation',
        outcome: 'blocked',
        redactions: 0,
        durationMs: 0,
        metadata: { errors: parsed.error.flatten() },
      });
      return {
        content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map(i => i.message).join(', ')}` }],
        isError: true,
      };
    }

    const { host, deviceType, command, params } = parsed.data;

    const hostPolicy = this.hostBlocked(args, host, command, deviceType);
    if (hostPolicy) return hostPolicy;

    // Validate parameters BEFORE touching the credential store: an invalid or
    // injected request must fail without decrypting anything or reaching a device.
    const validation = this.validator.validate(deviceType, command, params);
    if (!validation.valid) {
      this.auditWith(args, {
        timestamp: new Date().toISOString(),
        device: host,
        command,
        category: 'validation',
        outcome: 'blocked',
        redactions: 0,
        durationMs: 0,
        metadata: { reason: validation.error },
      });
      return {
        content: [{ type: 'text', text: `Validation error: ${validation.error}` }],
        isError: true,
      };
    }

    // A command that authenticates to a second device takes both of its credential params or neither (neither =
    // the pool fills them below). One without the other is refused before the store is touched.
    const poolAuth = validation.command!.poolAuthParams;
    const suppliedAuth = poolAuth ? poolAuth.filter((p) => params[p] !== undefined).length : 0;
    if (poolAuth && suppliedAuth === 1) {
      this.auditWith(args, {
        timestamp: new Date().toISOString(),
        device: host,
        command,
        category: 'validation',
        outcome: 'blocked',
        redactions: 0,
        durationMs: 0,
        metadata: { reason: 'partial credential params' },
      });
      return {
        content: [{ type: 'text', text: `Validation error: pass both ${poolAuth.join(' and ')} or neither (neither = the server uses the pool credentials)` }],
        isError: true,
      };
    }

    const storeData = await this.getStoreData();
    const resolver = new CredentialResolver(storeData);
    const credentials = resolver.resolveByType(deviceType);

    if (credentials.length === 0) {
      this.auditWith(args, {
        timestamp: new Date().toISOString(),
        device: host,
        command,
        category: 'auth',
        outcome: 'blocked',
        redactions: 0,
        durationMs: 0,
        metadata: { reason: 'no credentials for device type' },
      });
      return {
        content: [{ type: 'text', text: `No credentials found for device type: ${deviceType}. Use list-device-types to see available types.` }],
        isError: true,
      };
    }

    const policy = resolver.getPolicy(deviceType);
    const maxAttempts = policy?.maxAttempts ?? 5;
    const lastError = 'Authentication failed after policy-defined attempts';

    for (let i = 0; i < Math.min(credentials.length, maxAttempts); i++) {
      const credential = credentials[i];

      const client = createDeviceClient(host, credential, deviceType, this.config.limits.commandTimeoutMs);
      try {
        await client.connect();
        const result = poolAuth && suppliedAuth === 0
          ? await this.executeWithPoolAuth(client, validation.command!, params, credentials, i, maxAttempts)
          : await client.execute(validation.command!, params);
        await client.disconnect();

        const redacted = this.redaction.redact(JSON.stringify(result.result));

        this.auditWith(args, {
          timestamp: result.timestamp,
          device: host,
          command,
          category: result.category,
          outcome: result.status === 'success' ? 'success' : 'error',
          redactions: redacted.count,
          durationMs: result.durationMs,
        });

        let outputText = JSON.stringify({
          ...result,
          result: JSON.parse(redacted.text),
          redactions: redacted.count,
        }, null, 2);

        if (outputText.length > this.config.limits.maxOutputBytes) {
          outputText = outputText.substring(0, this.config.limits.maxOutputBytes) +
            `[truncated - output exceeded ${this.config.limits.maxOutputBytes} bytes]`;
        }

        return {
          content: [{
            type: 'text',
            text: outputText,
          }],
        };
      } catch (e) {
        //  lesson: NEVER label a connect failure as auth failure.
        // Timeouts/refused/unreachable mean the path failed, not the password;
        // conflating them sent a whole night of debugging after credentials.
        const msg = this.errText(e, credential);
        const transport = /timed out|timeout|ehostunreach|econnrefused|econnreset|handshake|network|socket|enotfound|eai_again|ehostdown|enetunreach/i.test(msg);
        this.auditWith(args, {
          timestamp: new Date().toISOString(),
          device: host,
          command,
          category: transport ? 'transport' : 'auth',
          outcome: transport ? 'transport_error' : 'auth_failed',
          redactions: 0,
          durationMs: 0,
          metadata: { profileIndex: i, error: msg.slice(0, 160) },
        });
        continue;
      }
    }

    return {
      content: [{ type: 'text', text: lastError }],
      isError: true,
    };
  }

  /** Runs a poolAuthParams command (bandwidth-test) on an open session, filling its credential params from the
   * pool: the credential that opened the session first, then the others in priority order, until the second
   * device stops answering "authentication failed". The passwords stay inside this process: they are neither
   * returned, logged nor audited, and the result only names which pool entry was accepted. */
  private async executeWithPoolAuth(
    client: { execute: (c: DeviceCommand, p: Record<string, unknown>) => Promise<CommandResult> },
    command: DeviceCommand,
    params: Record<string, unknown>,
    credentials: DeviceTypeCredential[],
    sessionIndex: number,
    maxAttempts: number,
  ): Promise<CommandResult> {
    const [userParam, passwordParam] = command.poolAuthParams!;
    const order = [sessionIndex, ...credentials.map((_, k) => k).filter((k) => k !== sessionIndex)].slice(0, Math.max(1, maxAttempts));
    let last: CommandResult | null = null;
    const skipped: number[] = [];
    for (const k of order) {
      const cred = credentials[k];
      const r = await client.execute(command, { ...params, [userParam]: cred.username, [passwordParam]: cred.password });
      if (r.status === 'error' && r.warnings.some((w) => /invalid format/.test(w))) { skipped.push(k); continue; }
      last = r;
      if (r.status === 'success' && !/authentication failed/i.test(JSON.stringify(r.result))) {
        return { ...r, warnings: [...r.warnings, `authenticated with pool credential #${k}`] };
      }
    }
    const note = skipped.length ? `; ${skipped.length} pool credential(s) have characters this command cannot pass` : '';
    if (last) return { ...last, warnings: [...last.warnings, `no pool credential was accepted by the test server${note}`] };
    return {
      device: '', command: command.name, category: command.category, status: 'error', result: null,
      warnings: [`no pool credential can be passed to ${command.name}${note}`], redactions: 0,
      timestamp: new Date().toISOString(), durationMs: 0,
    };
  }

  private async tempOspfPlan(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const plan = planTempOspf(args);
    this.auditWith(args, {
      timestamp: new Date().toISOString(),
      device: String((args as Record<string, unknown>).host ?? 'unknown'),
      command: 'temp-ospf-plan',
      category: 'planning',
      outcome: plan.valid ? 'success' : 'blocked',
      redactions: 0,
      durationMs: 0,
      metadata: { valid: plan.valid },
    });
    if (!plan.valid) return { content: [{ type: 'text', text: `Validation error: ${plan.error}` }], isError: true };
    return { content: [{ type: 'text', text: JSON.stringify({ ...plan, executes: false, note: 'Dry-run plan only. Live writes require the future gated executor and operator-run steps.' }, null, 2) }] };
  }

  private async tempRadioPlan(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const plan = planTempRadio(args);
    this.auditWith(args, {
      timestamp: new Date().toISOString(),
      device: String((args as Record<string, unknown>).host ?? 'unknown'),
      command: 'temp-radio-plan',
      category: 'planning',
      outcome: plan.valid ? 'success' : 'blocked',
      redactions: 0,
      durationMs: 0,
      metadata: { valid: plan.valid },
    });
    if (!plan.valid) return { content: [{ type: 'text', text: `Validation error: ${plan.error}` }], isError: true };
    return { content: [{ type: 'text', text: JSON.stringify({ ...plan, note: 'Dry-run plan only. Live writes require temp-radio-apply with all gates plus verified airOS transport.' }, null, 2) }] };
  }

  private async tempRadioApply(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const device = String((args as Record<string, unknown>).host ?? 'unknown');
    const baseAudit = { timestamp: new Date().toISOString(), device, command: 'temp-radio-apply', category: 'execution', redactions: 0, durationMs: 0 };
    if ((args as Record<string, unknown>).execute !== true) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'execute flag not set; returning plan' } });
      const plan = planTempRadio(args);
      return { content: [{ type: 'text', text: JSON.stringify({ ...plan, note: 'execute was not true: plan only, no write attempted.' }, null, 2) }], isError: !plan.valid };
    }
    const gates = checkExecuteGates();
    if (!gates.ok) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: gates.error } });
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const plan = planTempRadio(args);
    if (!plan.valid) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: plan.error } });
      return { content: [{ type: 'text', text: `Validation error: ${plan.error}` }], isError: true };
    }
    const { deviceId, host } = args as { deviceId: string; host: string };
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('ubnt-airos', String(deviceId), String(host));
    if (!credential) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'no unique exact-device infra binding' } });
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device credential is bound to this ID+host. Provision a per-incident infra binding via secure transfer; type-wide credentials are never used for writes.' }], isError: true };
    }
    const transport = checkTransportGates(String(host));
    if (!transport.ok) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: transport.error } });
      return { content: [{ type: 'text', text: `Blocked: ${transport.error}. Validated plan returned for operator-led execution.` }], isError: true };
    }
    if (!plan.op) {
      return { content: [{ type: 'text', text: 'Blocked: plan produced no op' }], isError: true };
    }
    if (plan.op.op === 'reboot') {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'reboot transport uses the same SSH path; run it from the safe-area test first' } });
      return { content: [{ type: 'text', text: 'Blocked: reboot transport not yet safe-area tested. Use operator-led reboot for now.' }], isError: true };
    }
    const startedAt = Date.now();
    const hostPolicy = this.hostBlocked(args, String(host), 'temp-radio-apply');
    if (hostPolicy) return hostPolicy;
    const snapshotId = snapshotName(plan.op.incidentId, String(host));
    try {
      const runner = new Ssh2Runner();
      // Secured-AP PSK (resolved at execute time only; the value never touches
      // plans, logs, or audit — metadata records the SOURCE alone).
      let stationPsk: string | undefined;
      let pskSource = 'none-open-profile';
      if (plan.op.op === 'set-mode' && plan.op.mode === 'managed' && plan.op.pskSource) {
        const src = plan.op.pskSource;
        try {
          if (src.type === 'ap-config') {
            if (!src.apHost || !src.apDeviceId) throw new Error('ap-config pskSource requires apHost and apDeviceId');
            const apPolicy = checkReadHost(src.apHost);
            if (!apPolicy.ok) throw new Error(apPolicy.error);
            const apCred = resolver.resolveExactDevice('ubnt-airos', src.apDeviceId, src.apHost);
            if (!apCred) throw new Error(`no exact-device binding for AP ${src.apDeviceId}@${src.apHost}; provision it or use env fallback`);
            const res = await resolveStationPsk({
              apHost: src.apHost,
              apRead: () => runner.exec(src.apHost, apCred, `grep -E '^aaa.1.wpa.psk=' /tmp/system.cfg`, this.config.limits.commandTimeoutMs),
            });
            stationPsk = res.psk;
            pskSource = res.source;
          } else {
            const res = await resolveStationPsk({ envIndex: src.index });
            stationPsk = res.psk;
            pskSource = res.source;
          }
        } catch (e) {
          this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: this.errText(e, credential) } });
          return { content: [{ type: 'text', text: `Blocked: PSK resolution failed (${this.errText(e, credential)}). No write attempted.` }], isError: true };
        }
      }
      const edits = opToEdits(plan.op, stationPsk);
      const preSnapshot = await runner.exec(String(host), credential, 'cat /tmp/system.cfg', this.config.limits.commandTimeoutMs);
      if (!preSnapshot.includes('wireless.1.ssid=')) throw new Error('Pre-apply snapshot is not an airOS system.cfg; refusing');
      // Private 0700 directory (verified owner, not a symlink), file 0600.
      writeSnapshotFile(snapshotId, preSnapshot);
      // Save-only apply: no post-save reboot (INC-C-01 live precedent — assoc
      // forms on softrestart save; INC-F-01 proved a reboot breaks verify+rollback).
      // The readback covers the keys this op actually edited (see readbackCommand).
      const result = await applyCfgEdits(runner, String(host), credential, edits, async () => {
        try {
          const check = await runner.exec(String(host), credential, readbackCommand(edits), 20000);
          return { ok: edits.every((e) => check.includes(`${e.key}=${e.value}`)), detail: redactCfgValues(check).trim().slice(0, 300) || 'empty readback' };
        } catch (e) {
          return { ok: false, detail: this.errText(e, credential) };
        }
      }, this.config.limits.commandTimeoutMs);
      this.auditWith(args, { ...baseAudit, outcome: result.applied ? 'success' : 'error', durationMs: Date.now() - startedAt, metadata: { op: plan.op.op, rolledBack: result.rolledBack, verifiedKeys: result.verifiedKeys, pskSource, snapshot: '<redacted-path>' } });
      return { content: [{ type: 'text', text: JSON.stringify({ ...result, confirmBy: plan.confirmBy, note: 'Keys persisted across reboot; snapshot kept owner-only for temp-radio-rollback. Association outcome must still be confirmed within the confirm window; roll back on any miss.' }, null, 2) }] };
    } catch (e) {
      this.auditWith(args, { ...baseAudit, outcome: 'error', durationMs: Date.now() - startedAt, metadata: { reason: this.errText(e, credential) } });
      return { content: [{ type: 'text', text: `Transport failed before save verification (${this.errText(e, credential)}). Inspect device state before retry; snapshot restore may be required.` }], isError: true };
    }
  }

  private async tempRadioRollback(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const { incidentId, host, deviceId } = args as { incidentId?: string; host?: string; deviceId?: string };
    const device = String(host ?? 'unknown');
    if (!incidentId || !host || !deviceId) {
      return { content: [{ type: 'text', text: 'Validation error: incidentId, host, and deviceId are required' }], isError: true };
    }
    const gates = checkExecuteGates();
    if (!gates.ok) {
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const snapshotId = snapshotName(incidentId, host);
    let snapshot: string | null;
    try {
      snapshot = readSnapshotFile(snapshotId);
    } catch (e) {
      return { content: [{ type: 'text', text: `Blocked: snapshot storage cannot be trusted (${this.errText(e)}); restore manually from the operator-held backup config` }], isError: true };
    }
    if (snapshot === null) {
      return { content: [{ type: 'text', text: 'Blocked: no snapshot on file for this incident+host; restore manually from the operator-held backup config' }], isError: true };
    }
    if (!snapshot.includes('wireless.1.ssid=')) {
      return { content: [{ type: 'text', text: 'Blocked: snapshot on file is not an airOS system.cfg; restore manually' }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('ubnt-airos', String(deviceId), String(host));
    if (!credential) {
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device credential is bound to this ID+host.' }], isError: true };
    }
    const hostPolicy = this.hostBlocked(args, String(host), 'temp-radio-rollback');
    if (hostPolicy) return hostPolicy;
    try {
      await restoreSnapshot(new Ssh2Runner(), String(host), credential, snapshot, this.config.limits.commandTimeoutMs);
      removeSnapshotFile(snapshotId); // keeps the snapshot if cleanup fails
      this.auditWith(args, { timestamp: new Date().toISOString(), device, command: 'temp-radio-rollback', category: 'execution', outcome: 'success', redactions: 0, durationMs: 0, metadata: { incidentId } });
      return { content: [{ type: 'text', text: JSON.stringify({ rolledBack: true, incidentId, host, note: 'Snapshot restored and saved (no reboot). Verify original role/clients before any further change.' }, null, 2) }] };
    } catch (e) {
      this.auditWith(args, { timestamp: new Date().toISOString(), device, command: 'temp-radio-rollback', category: 'execution', outcome: 'error', redactions: 0, durationMs: 0, metadata: { reason: this.errText(e, credential) } });
      return { content: [{ type: 'text', text: `Rollback FAILED (${this.errText(e, credential)}). Snapshot remains on file; escalate to operator with wired-side access.` }], isError: true };
    }
  }

  private async tempOspfApply(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const device = String((args as Record<string, unknown>).host ?? 'unknown');
    const baseAudit = { timestamp: new Date().toISOString(), device, command: 'temp-ospf-apply', category: 'execution', redactions: 0, durationMs: 0 };
    const parsed = TempOspfApplySchema.safeParse({ ...args, execute: (args as Record<string, unknown>).execute });
    if ((args as Record<string, unknown>).execute !== true) {
      const { planTempOspf } = await import('./temp-site/ospf.js');
      const plan = planTempOspf(args);
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'execute flag not set' } });
      return { content: [{ type: 'text', text: JSON.stringify({ ...plan, note: 'execute was not true: plan only.' }, null, 2) }], isError: !plan.valid };
    }
    if (!parsed.success) {
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map((i) => i.message).join(', ')}` }], isError: true };
    }
    const gates = checkRouterGates(parsed.data.host);
    if (!gates.ok) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: gates.error } });
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', parsed.data.deviceId, parsed.data.host);
    if (!credential) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'no unique exact-device router binding' } });
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host.' }], isError: true };
    }
    const startedAt = Date.now();
    const hostPolicy = this.hostBlocked(args, parsed.data.host, 'temp-ospf-apply');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(parsed.data.host, credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const run = (cmd: string) => client.runRaw(cmd);
      const result = await applyOspfAdjacency(run, parsed.data);
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { ...baseAudit, outcome: result.applied ? 'success' : 'error', durationMs: Date.now() - startedAt, metadata: { tag: result.tag, rolledBack: result.rolledBack } });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      this.auditWith(args, { ...baseAudit, outcome: 'error', durationMs: Date.now() - startedAt, metadata: { reason: this.errText(e, credential) } });
      return { content: [{ type: 'text', text: `OSPF apply failed (${this.errText(e, credential)}). Inspect router state before retry.` }], isError: true };
    }
  }

  private async tempOspfRollback(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const { incidentId, host, deviceId, routerOsVersion } = args as { incidentId?: string; host?: string; deviceId?: string; routerOsVersion?: 'ros6' | 'ros7' };
    if (!incidentId || !host || !deviceId) {
      return { content: [{ type: 'text', text: 'Validation error: incidentId, host, and deviceId are required' }], isError: true };
    }
    const gates = checkRouterGates(String(host));
    if (!gates.ok) {
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', String(deviceId), String(host));
    if (!credential) {
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host.' }], isError: true };
    }
    const hostPolicy = this.hostBlocked(args, String(host), 'temp-ospf-rollback');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(String(host), credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const result = await rollbackOspfAdjacency((cmd) => client.runRaw(cmd), `TEMP-${incidentId}-REMOVE-WHEN-REPAIRED`, routerOsVersion === 'ros6' ? 'ros6' : 'ros7');
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { timestamp: new Date().toISOString(), device: String(host), command: 'temp-ospf-rollback', category: 'execution', outcome: result.rolledBack ? 'success' : 'error', redactions: 0, durationMs: 0, metadata: { incidentId } });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      return { content: [{ type: 'text', text: `Rollback failed (${this.errText(e, credential)}). Inspect router state manually.` }], isError: true };
    }
  }

  private async tempMtFlip(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const a = args as Record<string, unknown>;
    const device = String(a.host ?? 'unknown');
    const baseAudit = { timestamp: new Date().toISOString(), device, command: 'temp-mt-flip', category: 'execution', redactions: 0, durationMs: 0 };
    if (a.execute !== true) {
      const parsed = TempMtFlipSchema.safeParse({ ...args, execute: false });
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'execute flag not set' } });
      return { content: [{ type: 'text', text: JSON.stringify({ valid: parsed.success, ...(parsed.success ? { op: 'mt-flip', confirmBy: new Date(Date.now() + 600000).toISOString() } : { error: 'validation failed' }), note: 'execute was not true: plan only.' }, null, 2) }], isError: !parsed.success };
    }
    const parsed = TempMtFlipSchema.safeParse(args);
    if (!parsed.success) {
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map((i) => i.message).join(', ')}` }], isError: true };
    }
    const gates = checkMtGates(parsed.data.host);
    if (!gates.ok) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: gates.error } });
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', parsed.data.deviceId, parsed.data.host);
    if (!credential) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'no unique exact-device MT binding' } });
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host.' }], isError: true };
    }
    const startedAt = Date.now();
    const hostPolicy = this.hostBlocked(args, parsed.data.host, 'temp-mt-flip');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(parsed.data.host, credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const run = (cmd: string) => client.runRaw(cmd);
      const result = await applyMtFlip(run, { ...parsed.data, execute: true });
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { ...baseAudit, outcome: result.applied ? 'success' : 'error', durationMs: Date.now() - startedAt, metadata: { previous: result.previous, rolledBack: result.rolledBack } });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      this.auditWith(args, { ...baseAudit, outcome: 'error', durationMs: Date.now() - startedAt, metadata: { reason: this.errText(e, credential) } });
      return { content: [{ type: 'text', text: `MT flip failed (${this.errText(e, credential)}). Inspect radio state before retry.` }], isError: true };
    }
  }

  private async tempMtRollback(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const a = args as { incidentId?: string; host?: string; deviceId?: string; iface?: string; mode?: string; ssid?: string; frequency?: string; protocol?: string; rateset?: string };
    if (!a.host || !a.deviceId || !a.mode || !a.ssid) {
      return { content: [{ type: 'text', text: 'Validation error: host, deviceId, mode, and ssid are required' }], isError: true };
    }
    const gates = checkMtGates(String(a.host));
    if (!gates.ok) {
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', String(a.deviceId), String(a.host));
    if (!credential) {
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host.' }], isError: true };
    }
    const hostPolicy = this.hostBlocked(args, String(a.host), 'temp-mt-rollback');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(String(a.host), credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const result = await rollbackMtFlip((cmd) => client.runRaw(cmd), { host: String(a.host), iface: a.iface, mode: String(a.mode), ssid: String(a.ssid), frequency: a.frequency, protocol: a.protocol, rateset: a.rateset });
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { timestamp: new Date().toISOString(), device: String(a.host), command: 'temp-mt-rollback', category: 'execution', outcome: result.rolledBack ? 'success' : 'error', redactions: 0, durationMs: 0, metadata: { incidentId: a.incidentId } });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      return { content: [{ type: 'text', text: `Rollback failed (${this.errText(e, credential)}). Inspect radio state manually.` }], isError: true };
    }
  }

  private async tempApWds(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const a = args as Record<string, unknown>;
    const device = String(a.host ?? 'unknown');
    const baseAudit = { timestamp: new Date().toISOString(), device, command: 'temp-ap-wds', category: 'execution', redactions: 0, durationMs: 0 };
    const parsed = TempApWdsSchema.safeParse(args);
    if (!parsed.success) {
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map((i) => i.message).join(', ')}` }], isError: true };
    }
    if (a.execute !== true) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'execute flag not set' } });
      return { content: [{ type: 'text', text: JSON.stringify({ valid: true, op: 'ap-wds', wdsMode: parsed.data.wdsMode, note: 'execute was not true: plan only.' }, null, 2) }] };
    }
    const gates = checkMtGates(parsed.data.host);
    if (!gates.ok) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: gates.error } });
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', parsed.data.deviceId, parsed.data.host);
    if (!credential) {
      this.auditWith(args, { ...baseAudit, outcome: 'blocked', metadata: { reason: 'no unique exact-device MT binding' } });
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host.' }], isError: true };
    }
    const hostPolicy = this.hostBlocked(args, parsed.data.host, 'temp-ap-wds');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(parsed.data.host, credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const result = await applyApWds((cmd) => client.runRaw(cmd), { ...parsed.data, execute: true });
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { ...baseAudit, outcome: result.applied ? 'success' : 'error', durationMs: 0, metadata: { previous: result.previous } });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      return { content: [{ type: 'text', text: `AP WDS change failed (${this.errText(e, credential)}).` }], isError: true };
    }
  }

  private async tempMtBounce(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const a = args as Record<string, unknown>;
    const parsed = TempMtBounceSchema.safeParse(args);
    if (!parsed.success) {
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map((i) => i.message).join(', ')}` }], isError: true };
    }
    if (a.execute !== true) {
      return { content: [{ type: 'text', text: JSON.stringify({ valid: true, op: 'mt-bounce', note: 'execute was not true: plan only.' }, null, 2) }] };
    }
    const gates = checkMtGates(parsed.data.host);
    if (!gates.ok) {
      this.auditWith(args, { timestamp: new Date().toISOString(), device: parsed.data.host, command: 'temp-mt-bounce', category: 'execution', outcome: 'blocked', redactions: 0, durationMs: 0, metadata: { reason: gates.error } });
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', parsed.data.deviceId, parsed.data.host);
    if (!credential) {
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host.' }], isError: true };
    }
    const hostPolicy = this.hostBlocked(args, parsed.data.host, 'temp-mt-bounce');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(parsed.data.host, credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const result = await applyMtBounce((cmd) => client.runRaw(cmd), { ...parsed.data, execute: true });
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { timestamp: new Date().toISOString(), device: parsed.data.host, command: 'temp-mt-bounce', category: 'execution', outcome: result.bounced ? 'success' : 'error', redactions: 0, durationMs: 0, metadata: {} });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      return { content: [{ type: 'text', text: `Bounce failed (${this.errText(e, credential)}).` }], isError: true };
    }
  }

  private async tempMtAlias(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const a = args as Record<string, unknown>;
    const parsed = TempMtAliasSchema.safeParse(args);
    if (!parsed.success) {
      return { content: [{ type: 'text', text: `Validation error: ${parsed.error.issues.map((i) => i.message).join(', ')}` }], isError: true };
    }
    if (a.execute !== true) {
      return { content: [{ type: 'text', text: JSON.stringify({ valid: true, op: 'mt-alias', note: 'execute was not true: plan only.' }, null, 2) }] };
    }
    const gates = checkMtGates(parsed.data.host);
    if (!gates.ok) {
      this.auditWith(args, { timestamp: new Date().toISOString(), device: parsed.data.host, command: 'temp-mt-alias', category: 'execution', outcome: 'blocked', redactions: 0, durationMs: 0, metadata: { reason: gates.error } });
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', parsed.data.deviceId, parsed.data.host);
    if (!credential) {
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host.' }], isError: true };
    }
    const hostPolicy = this.hostBlocked(args, parsed.data.host, 'temp-mt-alias');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(parsed.data.host, credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const result = await applyMtAlias((cmd) => client.runRaw(cmd), { ...parsed.data, execute: true });
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { timestamp: new Date().toISOString(), device: parsed.data.host, command: 'temp-mt-alias', category: 'execution', outcome: result.applied ? 'success' : 'error', redactions: 0, durationMs: 0, metadata: {} });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      return { content: [{ type: 'text', text: `Alias failed (${this.errText(e, credential)}).` }], isError: true };
    }
  }

  private async tempMtAliasRemove(args: Record<string, unknown>): Promise<{
    content: Array<{ type: 'text'; text: string }>;
    isError?: boolean;
  }> {
    const a = args as { incidentId?: string; host?: string; deviceId?: string };
    if (!a.host || !a.deviceId || !a.incidentId) {
      return { content: [{ type: 'text', text: 'Validation error: host, deviceId, and incidentId are required' }], isError: true };
    }
    const gates = checkMtGates(String(a.host));
    if (!gates.ok) {
      return { content: [{ type: 'text', text: `Blocked: ${gates.error}` }], isError: true };
    }
    const resolver = new CredentialResolver(await this.getStoreData());
    const credential = resolver.resolveExactDevice('mikrotik-ssh', String(a.deviceId), String(a.host));
    if (!credential) {
      return { content: [{ type: 'text', text: 'Blocked: no unique exact-device mikrotik-ssh credential is bound to this ID+host.' }], isError: true };
    }
    const hostPolicy = this.hostBlocked(args, String(a.host), 'temp-mt-alias-remove');
    if (hostPolicy) return hostPolicy;
    const client = createDeviceClient(String(a.host), credential, 'mikrotik-ssh', this.config.limits.commandTimeoutMs) as MikroTikClient;
    try {
      await client.connect();
      const result = await removeMtAlias((cmd) => client.runRaw(cmd), { host: String(a.host), incidentId: String(a.incidentId) });
      await client.disconnect().catch(() => { /* best effort */ });
      this.auditWith(args, { timestamp: new Date().toISOString(), device: String(a.host), command: 'temp-mt-alias-remove', category: 'execution', outcome: result.removed ? 'success' : 'error', redactions: 0, durationMs: 0, metadata: { incidentId: a.incidentId } });
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (e) {
      try { await client.disconnect(); } catch { /* noop */ }
      return { content: [{ type: 'text', text: `Alias removal failed (${this.errText(e, credential)}). Inspect radio state manually.` }], isError: true };
    }
  }

  private async listDevices(): Promise<{
    content: Array<{ type: 'text'; text: string }>;
  }> {
    const storeData = await this.getStoreData();
    const resolver = new CredentialResolver(storeData);
    const deviceTypes = resolver.listDeviceTypes();

    const devices = deviceTypes.map(deviceType => {
      const group = storeData.deviceTypes.find(g => g.deviceType === deviceType);
      const commands = this.validator.listCommands(deviceType);

      return {
        deviceType,
        credentialCount: group?.credentials.length ?? 0,
        commands,
      };
    });

    const output = JSON.stringify(devices, null, 2);
    const redacted = this.redaction.redact(output);

    return {
      content: [{ type: 'text', text: redacted.text }],
    };
  }

  private async getStoreData(): Promise<CredentialStoreData> {
    if (!this.storeData) {
      const passphrase = process.env[this.config.credentialStore.passphraseEnvVar];
      if (!passphrase) {
        const fb = getRepoEnvStatus();
        throw new Error(
          `Missing passphrase: set ${this.config.credentialStore.passphraseEnvVar} environment variable` +
            (fb.path
              ? ` (repo .env fallback already applied from ${fb.path}, filled ${fb.filled} var(s) — add the passphrase there or export it before launching)`
              : ' (no repo .env found via NOC_MCP_DOTENV, ./.env or ../.env — add it or export the passphrase before launching)')
        );
      }
      this.credentialStore = new CredentialStore(this.config.credentialStore.path, passphrase);
      this.storeData = await this.credentialStore.load();
    }
    return this.storeData;
  }
}
