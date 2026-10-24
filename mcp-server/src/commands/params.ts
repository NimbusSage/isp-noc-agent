/**
 * Parameter validation and template rendering for read-only device commands.
 *
 * Root cause of the audit finding: only `typeof` was checked and
 * `String(value)` was pasted into RouterOS templates, so
 * address="8.8.8.8 ; /user add name=x group=full" produced a multi-command line.
 *
 * Defence in depth: the CommandValidator calls validateParamValue() on the
 * request, AND every client's buildCommand() calls renderTemplate(), which
 * validates again. A caller that skips the validator still cannot inject.
 */
import type { DeviceCommand, ParamSpec } from '../devices/types.js';

/** Characters that can never appear in a string parameter, whatever its schema. */
const GLOBAL_STRING_REJECT = /[;\n\r[\]$"'`\\|\x00-\x1f\x7f]/;
const DEFAULT_MAX_STRING = 128;

export function validateParamValue(name: string, spec: ParamSpec, value: unknown): string | null {
  if (typeof value !== spec.type) {
    return `Parameter '${name}' must be ${spec.type}, got ${typeof value}`;
  }
  if (spec.type === 'boolean') return null;

  if (spec.type === 'number') {
    const n = value as number;
    if (!Number.isFinite(n)) return `Parameter '${name}' must be a finite number`;
    if (spec.integer && !Number.isSafeInteger(n)) return `Parameter '${name}' must be an integer`;
    if (spec.min === undefined || spec.max === undefined) return `Parameter '${name}' has no numeric range in its schema; refusing`;
    if (n < spec.min || n > spec.max) return `Parameter '${name}' must be between ${spec.min} and ${spec.max}`;
    return null;
  }

  const s = value as string;
  if (s.length === 0) return `Parameter '${name}' must not be empty`;
  if (s.length > (spec.maxLength ?? DEFAULT_MAX_STRING)) return `Parameter '${name}' is too long`;
  if (GLOBAL_STRING_REJECT.test(s)) {
    return `Parameter '${name}' contains a forbidden character (; newline [ ] $ " ' \` \\ | or a control character)`;
  }
  if (!spec.pattern && !spec.enum && !spec.check) {
    return `Parameter '${name}' has no validation schema; refusing`;
  }
  if (spec.enum && !spec.enum.includes(s)) return `Parameter '${name}' must be one of: ${spec.enum.join(', ')}`;
  if (spec.pattern && !spec.pattern.test(s)) return `Parameter '${name}' has an invalid format`;
  if (spec.check) {
    const problem = spec.check(s);
    if (problem) return `Parameter '${name}': ${problem}`;
  }
  return null;
}

/**
 * Substitute {{param}} placeholders. Only parameters declared in the command's
 * schema are substituted, every value (including defaults) is validated, and
 * leftover placeholders are an error. `encode` lets URL clients percent-encode.
 */
export function renderTemplate(
  command: DeviceCommand,
  params: Record<string, unknown>,
  encode: (value: string) => string = (v) => v,
): string {
  let out = command.template;
  for (const [key, spec] of Object.entries(command.params)) {
    const supplied = Object.prototype.hasOwnProperty.call(params, key) ? params[key] : undefined;
    const value = supplied ?? spec.default;
    if (value === undefined) {
      if (spec.required) throw new Error(`Missing required parameter '${key}' for command '${command.name}'`);
      continue;
    }
    const problem = validateParamValue(key, spec, value);
    if (problem) throw new Error(problem);
    out = out.split(`{{${key}}}`).join(encode(String(value)));
  }
  if (/\{\{[^}]*\}\}/.test(out)) {
    throw new Error(`Unresolved placeholder in command '${command.name}'`);
  }
  return out;
}
