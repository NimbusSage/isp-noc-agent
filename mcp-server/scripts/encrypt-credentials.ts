import { readFileSync, writeFileSync } from 'fs';
import { encryptCredentialStore, type StoreFormat } from '../src/credentials/store.js';
import { CredentialStoreSchema } from '../src/credentials/schema.js';

// Usage: tsx scripts/encrypt-credentials.ts [--legacy] <input.json>
//   default   writes the NOCENC2 format (scrypt N=2^15 r=8 p=1, random salt, header bound as AAD)
//   --legacy  writes the old unsalted sha256-key format, for tools that cannot read NOCENC2 yet
// The reader (src/credentials/store.ts) accepts both. This script only ever runs when invoked
// explicitly (npm run encrypt); nothing migrates an existing credentials.enc automatically.
const passphrase = process.env.NOC_MCP_PASSPHRASE;
if (!passphrase) {
  console.error('Set NOC_MCP_PASSPHRASE environment variable');
  process.exit(1);
}

const args = process.argv.slice(2);
const legacy = args.includes('--legacy');
const inputPath = args.find((a) => !a.startsWith('--'));
if (!inputPath) {
  console.error('Usage: tsx scripts/encrypt-credentials.ts [--legacy] <input.json>');
  process.exit(1);
}

const format: StoreFormat = legacy ? 'legacy' : 'v2';
const plaintext = readFileSync(inputPath, 'utf-8');
const data = JSON.parse(plaintext);
const validated = CredentialStoreSchema.parse(data);
const encrypted = encryptCredentialStore(validated, passphrase, { format });

const outputPath = inputPath.replace(/\.json$/, '.enc');
writeFileSync(outputPath, encrypted, { mode: 0o600 });
console.log(`Encrypted credentials written to ${outputPath}`);
console.log(format === 'v2'
  ? 'Format: NOCENC2 (scrypt N=32768 r=8 p=1, random 16-byte salt, AES-256-GCM, header authenticated)'
  : 'Format: legacy (unsalted sha256 key, AES-256-GCM) -- weaker; use only for tools that cannot read NOCENC2');
