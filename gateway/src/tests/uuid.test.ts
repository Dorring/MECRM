import { uuidv4, uuidv5 } from '../utils/uuid';

describe('UUID utilities', () => {
  it('generates RFC 4122 version 4 UUIDs', () => {
    expect(uuidv4()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
    );
  });

  it('matches the RFC 4122 version 5 DNS vector', () => {
    const dnsNamespace = '6ba7b810-9dad-11d1-80b4-00c04fd430c8';
    expect(uuidv5('www.example.com', dnsNamespace)).toBe(
      '2ed6657d-e927-568b-95e1-2665a8aea6a2'
    );
  });

  it('rejects invalid namespaces', () => {
    expect(() => uuidv5('value', 'invalid')).toThrow(TypeError);
  });
});
