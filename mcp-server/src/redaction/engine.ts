import { DEFAULT_PATTERNS, type RedactionPattern } from './patterns.js';

interface RedactionConfig {
  enabled: boolean;
  preserveInternalIps: string[];
  preserveHostnames: string[];
}

interface RedactionResult {
  text: string;
  count: number;
}

export class RedactionEngine {
  private config: RedactionConfig;
  private patterns: RedactionPattern[];

  constructor(config: RedactionConfig, patterns?: RedactionPattern[]) {
    this.config = config;
    this.patterns = patterns ?? DEFAULT_PATTERNS;
  }

  redact(text: string): RedactionResult {
    if (!this.config.enabled) {
      return { text, count: 0 };
    }

    let result = text;
    let totalRedactions = 0;

    // Apply general patterns
    for (const pattern of this.patterns) {
      const matches = result.match(pattern.regex);
      if (matches) {
        totalRedactions += matches.length;
        result = result.replace(pattern.regex, pattern.replacement);
      }
    }

    // Redact hostnames not in preserve list
    // Collect all hostname matches first to avoid mutating string during regex iteration
    const hostnameRegex = /\b([a-z0-9]+(?:-[a-z0-9]+)*\.(?:[a-z]{2,}|internal|local|lan))\b/gi;
    const hostnameMatches: Array<{ hostname: string; index: number; length: number }> = [];
    let hostnameMatch;
    while ((hostnameMatch = hostnameRegex.exec(result)) !== null) {
      hostnameMatches.push({
        hostname: hostnameMatch[1],
        index: hostnameMatch.index,
        length: hostnameMatch[0].length,
      });
    }

    // Apply replacements in reverse order to preserve indices
    for (let i = hostnameMatches.length - 1; i >= 0; i--) {
      const { hostname } = hostnameMatches[i];
      if (!this.config.preserveHostnames.includes(hostname)) {
        const escaped = hostname.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const hostRegex = new RegExp(escaped, 'gi');
        const count = (result.match(hostRegex) || []).length;
        result = result.replace(hostRegex, '[REDACTED_HOST]');
        totalRedactions += count;
      }
    }

    return { text: result, count: totalRedactions };
  }
}
