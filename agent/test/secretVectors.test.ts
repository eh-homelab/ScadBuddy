import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { secretVectors } from '../scripts/gen-secret-vectors.js'
import { kekFromBase64, openSecret } from '../src/secrets.js'

const committed = JSON.parse(readFileSync(new URL('./fixtures/secret-vectors.json', import.meta.url), 'utf8'))

describe('secret vectors (spec §6.2)', () => {
  it('regenerates byte for byte: a format or AAD change needs new vectors', () => {
    expect(secretVectors()).toEqual(committed)
  })
  it('covers every credential kind in v2 and the v1 format', () => {
    const names = committed.vectors.map((v: { name: string }) => v.name)
    expect(names).toEqual(expect.arrayContaining(['anthropic_api_key.v2', 'claude_oauth_token.v2', 'gateway.v2', 'v1']))
  })
  it('opens every vector with the TypeScript openSecret', () => {
    const kek = kekFromBase64(committed.kek_base64)
    for (const v of committed.vectors) {
      const envelope = {
        secretSealed: Buffer.from(v.secret_sealed_b64, 'base64'),
        dekSealed: Buffer.from(v.dek_sealed_b64, 'base64'),
        kekId: v.kek_id,
      }
      expect(openSecret(kek, envelope, v.aad)).toBe(v.plaintext)
    }
  })
})
