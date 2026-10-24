export interface RedactionPattern {
  name: string;
  regex: RegExp;
  replacement: string;
}

export const DEFAULT_PATTERNS: RedactionPattern[] = [
  // NOTE (operator policy): IPs and MACs in command output are
  // NOT redacted. They are the operational payload of diagnostics
  // (ppp/ARP/DHCP tables, registration lists) and nearly always RFC 1918.
  // Secrets below stay redacted.
  {
    name: 'password-quoted',
    regex: /(password|passwd|pwd)\s*[=:]\s*["']?([^\s"']+)/gi,
    replacement: '$1=[REDACTED]',
  },
  {
    name: 'snmp-community',
    regex: /(community|snmp_community)\s*[=:]\s*["']?([^\s"']+)/gi,
    replacement: '$1=[REDACTED]',
  },
  {
    name: 'api-token',
    regex: /(x-auth-token|api-key|apikey|token)\s*[=:]\s*["']?([^\s"']+)/gi,
    replacement: '$1=[REDACTED]',
  },
  {
    name: 'serial-number',
    regex: /\b(S\/?N|serial)[:\s]+([A-Z0-9]{8,})\b/gi,
    replacement: '$1=[REDACTED_SN]',
  },
  {
    name: 'private-key',
    regex: /-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(RSA\s+)?PRIVATE\s+KEY-----/g,
    replacement: '[REDACTED_PRIVATE_KEY]',
  },
  // ---- Lane 2b additions. Output reaches the redactor as JSON.stringify()'d
  // text, so newlines are the two characters \n and nested JSON has \" quotes;
  // every pattern below is written to survive both. Existing patterns above
  // are unchanged. New patterns skip values that are already [REDACTED] so
  // nothing is double-counted.
  {
    // {"password":"x"}, {"psk":"x"}, "api_key":"x", "wpa_psk":"x", ...
    name: 'json-secret-quoted',
    regex: /("(?:[A-Za-z0-9_-]{0,64}(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|psk|private[_-]?key|community)[A-Za-z0-9_-]{0,64})"\s*:\s*")(?!\[REDACTED)((?:[^"\\]|\\.)*)(")/gi,
    replacement: '$1[REDACTED]$3',
  },
  {
    // The same inside a JSON string that was stringified again: \"password\":\"x\"
    name: 'json-secret-escaped',
    regex: /(\\"(?:[A-Za-z0-9_-]{0,64}(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key|psk|private[_-]?key|community)[A-Za-z0-9_-]{0,64})\\"\s*:\s*\\")(?!\[REDACTED)((?:(?!\\").)*)(\\")/gi,
    replacement: '$1[REDACTED]$3',
  },
  {
    // key=value / key: value for secret-ish keys, including dotted/hyphenated
    // names: secret=, passphrase=, pre-shared-key=, wpa-psk=, wpa2-pre-shared-key=,
    // wpasec.psk=, radio.1.psk=, aaa.1.wpa.psk=, wpasupplicant.profile.1.network.1.psk=,
    // users.1.password=... Quoted values may contain spaces.
    name: 'secret-assignment',
    // Bounded prefix/suffix + a token-start lookbehind keep this linear-time on
    // hostile device output (an unbounded nested quantifier here was a ReDoS).
    regex: /(?<![A-Za-z0-9._-])([A-Za-z0-9._-]{0,64}?(?:pre-shared-key|presharedkey|psk|passphrase|passwd|password|pwd|secret|api[_-]?key|apikey|private[_-]?key|snmp[_-]?community|community|token)[A-Za-z0-9._-]{0,64})(\s*[=:]\s*)(?!\[REDACTED)("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\\"(?:(?!\\").)*\\"|[^\s"'\\]+)/gi,
    replacement: '$1$2[REDACTED]',
  },
  {
    // Authorization: Bearer xxxx / Basic xxxx / Token xxxx
    name: 'authorization-header',
    regex: /\b(authorization)(\s*[=:]\s*)(?!\[REDACTED)(?:(bearer|basic|token|digest)\s+)?[^\s"'\\]+/gi,
    replacement: '$1$2[REDACTED]',
  },
  {
    // Stray bearer tokens outside a header line.
    name: 'bearer-token',
    regex: /\b(bearer)\s+(?!\[REDACTED)[A-Za-z0-9._~+/=-]{8,}/gi,
    replacement: '$1 [REDACTED]',
  },
  {
    // OPENSSH / EC / DSA / ENCRYPTED / PKCS8 / PGP private keys (the legacy
    // pattern above only knew "PRIVATE KEY" and "RSA PRIVATE KEY").
    name: 'private-key-block',
    regex: /-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----/g,
    replacement: '[REDACTED_PRIVATE_KEY]',
  },
  {
    // A private-key block cut off before its END line (truncated output).
    name: 'private-key-unterminated',
    regex: /-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----(?![\s\S]*-----END)[\s\S]*$/g,
    replacement: '[REDACTED_PRIVATE_KEY]',
  },
];

// preserveInternalIps in server.json is inert (no IP
// patterns remain); kept in the config schema for backward compatibility.
