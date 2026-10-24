import { COMMAND_REGISTRY } from './allowlist.js';
import { validateParamValue } from './params.js';
import type { DeviceCommand } from '../devices/types.js';

const has = (obj: object, key: string): boolean => Object.prototype.hasOwnProperty.call(obj, key);

interface ValidationResult {
  valid: boolean;
  command?: DeviceCommand;
  error?: string;
}

export class CommandValidator {
  validate(deviceType: string, commandName: string, params: Record<string, unknown>): ValidationResult {
    const commands = has(COMMAND_REGISTRY, deviceType) ? COMMAND_REGISTRY[deviceType] : undefined;
    if (!commands) {
      return { valid: false, error: `Unknown device type: ${deviceType}. Allowed: ${Object.keys(COMMAND_REGISTRY).join(', ')}` };
    }

    const command = has(commands, commandName) ? commands[commandName] : undefined;
    if (!command) {
      return { valid: false, error: `Command '${commandName}' not allowed for ${deviceType}. Available: ${Object.keys(commands).join(', ')}` };
    }

    if (!command.readOnly) {
      return { valid: false, error: `Command '${commandName}' is not read-only and is blocked` };
    }

    for (const [key, value] of Object.entries(params)) {
      const spec = has(command.params, key) ? command.params[key] : undefined;
      if (!spec) {
        return { valid: false, error: `Unknown parameter '${key}' for command '${commandName}'` };
      }
      const problem = validateParamValue(key, spec, value);
      if (problem) {
        return { valid: false, error: problem };
      }
    }

    for (const [key, spec] of Object.entries(command.params)) {
      if (spec.required && !(key in params) && spec.default === undefined) {
        return { valid: false, error: `Missing required parameter '${key}' for command '${commandName}'` };
      }
    }

    return { valid: true, command };
  }

  listCommands(deviceType: string): string[] {
    const commands = has(COMMAND_REGISTRY, deviceType) ? COMMAND_REGISTRY[deviceType] : undefined;
    if (!commands) return [];
    return Object.keys(commands);
  }

  getCommand(deviceType: string, commandName: string): DeviceCommand | null {
    if (!has(COMMAND_REGISTRY, deviceType)) return null;
    const commands = COMMAND_REGISTRY[deviceType];
    return has(commands, commandName) ? commands[commandName] : null;
  }
}
