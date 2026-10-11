import { readFileSync, writeFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { approvalHashKey, canonicalJson, inputHash } from '../src/approvals/service.js'
import type { Kek } from '../src/secrets.js'

// The input hash a flow run's approval binds to (#2318) is computed by the backend
// (backend/scadbuddy/flows/approval_hash.py) and checked here, by PgFlowRuns. These
// vectors pin the two together: each input is JSON text that both sides parse as they
// parse a payload, and backend/tests/test_approval_hash.py checks every one against
// its copy in backend/tests/fixtures. Regenerate (then copy it there):
// UPDATE_APPROVAL_HASH_VECTORS=1 pnpm vitest run test/approvalHashVectors.test.ts
const FILE = new URL('./fixtures/approval-hash-vectors.json', import.meta.url)

const INPUTS: Array<[string, string]> = [
  ['render', '{"model":"box","params":{"width":20,"depth":1.0,"label":"Lid"}}'],
  ['render', '{"params":{"tiny":1e-7,"small":0.0000123,"big":1.5e22,"edge":1e21,"under":1e20,"neg":-0.0}}'],
  ['write', '{"text":"pink \\u2028 line\\u0001 \\"q\\" \\\\ tab\\t","emoji":"\\ud83d\\ude00","lone":"\\ud800x"}'],
  ['write', '{"\\ue000":1,"\\ud83d\\ude00":2,"b":[1,2,{"z":null,"a":true}],"a":false}'],
  ['count', '{"n":9007199254740993,"m":1152921504606846976,"f":3.14,"z":0}'],
  ['empty', '{}'],
]

describe('approval hash vectors', () => {
  it('match what inputHash computes', () => {
    const kekKey = Buffer.alloc(32, 7)
    const key = approvalHashKey({ id: 'k1', key: kekKey } as Kek)
    const cases = INPUTS.map(([tool, json]) => {
      const input = JSON.parse(json) as Record<string, unknown>
      return { tool, input_json: json, canonical: canonicalJson({ tool, input }), hash: inputHash(key, tool, input) }
    })
    const text = `${JSON.stringify({ kek_hex: kekKey.toString('hex'), hash_key_hex: key.toString('hex'), cases }, null, 2)}\n`
    if (process.env.UPDATE_APPROVAL_HASH_VECTORS === '1') writeFileSync(FILE, text)
    expect(readFileSync(FILE, 'utf8')).toBe(text)
  })
})
