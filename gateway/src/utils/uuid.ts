import { createHash, randomUUID } from 'crypto';

const UUID_HEX_LENGTH = 32;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const uuidv4 = (): string => randomUUID();

export const uuidv5 = (value: string, namespace: string): string => {
  if (!UUID_PATTERN.test(namespace)) {
    throw new TypeError('UUID v5 namespace must be a valid UUID');
  }

  const namespaceHex = namespace.replace(/-/g, '');
  if (namespaceHex.length !== UUID_HEX_LENGTH) {
    throw new TypeError('UUID v5 namespace must contain 16 bytes');
  }

  // RFC 4122 UUID v5 mandates SHA-1 for deterministic names. This is not
  // used for passwords, signatures, tokens, or any security decision.
  const digest = createHash('sha1')
    .update(Buffer.from(namespaceHex, 'hex'))
    .update(value, 'utf8')
    .digest();

  digest[6] = (digest[6] & 0x0f) | 0x50;
  digest[8] = (digest[8] & 0x3f) | 0x80;

  const hex = digest.subarray(0, 16).toString('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
};
