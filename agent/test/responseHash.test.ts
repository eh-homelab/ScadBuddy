import { readFileSync, writeFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { canonicalJson, responseHash } from '../src/gate/canonical.js'

// The hash of a response that ai_input_responses.response_hash keeps (#3724). The
// vectors are the one source of truth for it: this test fails if the committed file
// differs from what canonical.ts computes, and agent-durable's
// tests/test_gate_canonical.py computes every one again from gate/canonical.py.
// Regenerate: UPDATE_RESPONSE_HASH_VECTORS=1 pnpm vitest run test/responseHash.test.ts
const FILE = new URL('./fixtures/response-hash-vectors.json', import.meta.url)

const CASES: { name: string; value: unknown }[] = [
  { name: 'an approval', value: { kind: 'approval', decision: 'approve' } },
  { name: 'key order does not matter', value: { decision: 'approve', kind: 'approval' } },
  { name: 'an approval with an input hash', value: { kind: 'approval', decision: 'deny', input_hash: 'a'.repeat(64) } },
  { name: 'an answer keyed by question text', value: { kind: 'answer', answers: { 'Which colour?': 'red', 'Sizes?': ['S', 'M'] } } },
  { name: 'an attention choice', value: { kind: 'answer', choice: 'proceed' } },
  { name: 'escapes and control characters', value: { kind: 'answer', text: 'a "quote" \\ back\nnew\ttab\u0001\u001f end' } },
  { name: 'non-ASCII text is not escaped', value: { kind: 'answer', text: 'café   é中' } },
  { name: 'astral keys sort by UTF-16 code unit, not code point', value: { kind: 'answer', answers: { '😀': 'a', '～': 'b', z: 'c' } } },
  { name: 'booleans, null and nesting', value: { a: [true, false, null, { b: [] }], c: {} } },
  { name: 'a bare string', value: 'x' },
  // JSON.stringify escapes an unpaired surrogate (well-formed JSON); canonical.py must too.
  { name: 'a lone surrogate is escaped, not encoded', value: { kind: 'answer', text: 'a\udc00b\ud83d' } },
  { name: 'a lone surrogate in a key is escaped and sorts by code unit', value: { kind: 'answer', answers: { '\udc00': 'a', '\ud800x': 'b', z: 'c' } } },
]

const built = {
  note: 'canonical JSON and its sha256, written by test/responseHash.test.ts (UPDATE_RESPONSE_HASH_VECTORS=1), opened by agent-durable/tests/test_gate_canonical.py',
  vectors: CASES.map((c) => ({ ...c, canonical: canonicalJson(c.value), sha256: responseHash(c.value) })),
}

describe('the response hash vectors (#3724)', () => {
  it('match the committed file', () => {
    if (process.env.UPDATE_RESPONSE_HASH_VECTORS === '1') writeFileSync(FILE, `${JSON.stringify(built, null, 2)}\n`)
    expect(JSON.parse(readFileSync(FILE, 'utf8'))).toEqual(built)
  })
  it('refuses a number, which JS and Python would print differently', () => {
    for (const n of [1, -0, 1e21, 1.5]) expect(() => canonicalJson({ kind: 'answer', n })).toThrow(TypeError)
  })
  it('hash a response the same whatever its key order', () => {
    expect(responseHash({ kind: 'approval', decision: 'approve' })).toBe(responseHash({ decision: 'approve', kind: 'approval' }))
    expect(responseHash({ kind: 'approval', decision: 'deny' })).not.toBe(responseHash({ kind: 'approval', decision: 'approve' }))
  })
})
