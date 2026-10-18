import { createHash } from 'node:crypto'

// Canonical JSON and the hash of a response (#3717, #3724). agent-durable's
// gate/canonical.py is the same function; test/fixtures/response-hash-vectors.json
// pins the two together. Keys sorted by UTF-16 code unit, no whitespace; a response
// holds only strings, booleans, null, arrays and objects (a number is refused).

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    const o = value as Record<string, unknown>
    return `{${Object.keys(o)
      .filter((k) => o[k] !== undefined)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonicalJson(o[k])}`)
      .join(',')}}`
  }
  // JS and Python print some numbers differently (1e21, -0, 1.0), and no response holds
  // one: refused, so a future numeric field cannot split the two hashes unnoticed.
  if (typeof value === 'number') throw new TypeError('a response holds no numbers, and canonical JSON has none')
  return JSON.stringify(value) ?? 'null'
}

/** sha256 hex of the canonical JSON of a response. */
export function responseHash(response: unknown): string {
  return createHash('sha256').update(canonicalJson(response)).digest('hex')
}
