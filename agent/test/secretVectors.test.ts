import { createCipheriv, createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { credentialAad, LEGACY_FORMAT_MESSAGE, openCredential, type CredentialKind } from '../src/credentials.js'
import { kekFromBase64, openSecret, sealSecret, type RandomSource } from '../src/secrets.js'

// The one source of truth for the sealed format (spec 2026-10-01 §6.2, "Test
// vectors"). This test seals fixed plaintexts with fixed bytes and fails if the
// committed file differs, so secrets.ts's format or credentials.ts's AAD cannot
// change without new vectors. agent-durable/tests/test_secrets.py and
// test_credentials.py open every one, so new vectors cannot land without the
// Python port opening them. Regenerate: UPDATE_SECRET_VECTORS=1 pnpm vitest run test/secretVectors.test.ts
const FILE = new URL('./fixtures/secret-vectors.json', import.meta.url)

/** A counter, never random: reproducible bytes for the vectors only. */
function counter(seed: number): RandomSource {
  let n = seed
  return (size) => {
    const out = Buffer.alloc(size)
    for (let i = 0; i < size; i++) out[i] = (n++ * 131 + 7) & 0xff
    return out
  }
}

const KEK_B64 = Buffer.alloc(32, 0x5a).toString('base64')
const kek = kekFromBase64(KEK_B64)

const CREDENTIALS: { name: string; id: string; priority: number; kind: CredentialKind; base_url: string | null; plaintext: string }[] = [
  { name: 'api key, migrated row id', id: 'default', priority: 0, kind: 'anthropic_api_key', base_url: null, plaintext: 'sk-ant-api03-vector-0000000000000000' },
  { name: 'oauth token', id: 'c0ffee00-0000-4000-8000-000000000001', priority: 1, kind: 'claude_oauth_token', base_url: null, plaintext: 'sk-ant-oat01-vector-1111111111111111' },
  { name: 'gateway with a path', id: 'c0ffee00-0000-4000-8000-000000000002', priority: 2, kind: 'gateway', base_url: 'https://gw.example.com/anthropic/v1', plaintext: 'gw-token-vector-2222222222222222' },
  // Review Focus 1: JSON.stringify leaves non-ASCII as is; the port must too.
  { name: 'gateway, non-ASCII host', id: 'c0ffee00-0000-4000-8000-000000000003', priority: 3, kind: 'gateway', base_url: 'https://passerelle.exemple.fr/é/v1', plaintext: 'gw-token-vector-3333333333333333' },
]

/** A version-1 envelope as #354 wrote it (AAD = the bare context). Frozen: seal never writes v1. */
function sealV1(key: Buffer, plaintext: Buffer, context: string, random: RandomSource): Buffer {
  const iv = random(12)
  const cipher = createCipheriv('aes-256-gcm', key, iv)
  cipher.setAAD(Buffer.from(context, 'utf8'))
  const ct = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return Buffer.concat([Buffer.from([0x01]), iv, cipher.getAuthTag(), ct])
}

function build() {
  const credentials = CREDENTIALS.map((c, i) => {
    const env = sealSecret(kek, c.plaintext, credentialAad(c.id, c.kind, c.base_url), counter(1000 * (i + 1)))
    return {
      name: c.name, id: c.id, priority: c.priority, kind: c.kind, base_url: c.base_url,
      secret_sealed_b64: env.secretSealed.toString('base64'), dek_sealed_b64: env.dekSealed.toString('base64'),
      kek_id: env.kekId, plaintext: c.plaintext, opens: true,
    }
  })
  // A v1 credential: openCredential refuses it with LEGACY_FORMAT_MESSAGE.
  const r = counter(9000)
  const legacyAad = credentialAad('c0ffee00-0000-4000-8000-000000000009', 'anthropic_api_key', null)
  const dek = r(32)
  credentials.push({
    name: 'v1 credential (refused)', id: 'c0ffee00-0000-4000-8000-000000000009', priority: 9, kind: 'anthropic_api_key', base_url: null,
    secret_sealed_b64: sealV1(dek, Buffer.from('sk-ant-legacy'), legacyAad, r).toString('base64'),
    dek_sealed_b64: sealV1(kek.key, dek, `dek:${legacyAad}`, r).toString('base64'),
    kek_id: kek.id, plaintext: 'sk-ant-legacy', opens: false,
  })
  const r2 = counter(7000)
  const dek2 = r2(32)
  const secrets = [{
    name: 'v1 secret', aad: 'ai_credentials:default', version: 1,
    secret_sealed_b64: sealV1(dek2, Buffer.from('v1-plain'), 'ai_credentials:default', r2).toString('base64'),
    dek_sealed_b64: sealV1(kek.key, dek2, 'dek:ai_credentials:default', r2).toString('base64'),
    kek_id: kek.id, plaintext: 'v1-plain',
  }]
  return { kek_b64: KEK_B64, kek_id: createHash('sha256').update(kek.key).digest('hex').slice(0, 16), credentials, secrets }
}

describe('secret vectors', () => {
  const built = build()

  it('match the committed file', () => {
    if (process.env.UPDATE_SECRET_VECTORS === '1') writeFileSync(FILE, `${JSON.stringify(built, null, 2)}\n`)
    expect(JSON.parse(readFileSync(FILE, 'utf8'))).toEqual(built)
  })

  it('open with the agent itself', () => {
    for (const v of built.credentials) {
      const row = {
        id: v.id, kind: v.kind, base_url: v.base_url,
        envelope: { secretSealed: Buffer.from(v.secret_sealed_b64, 'base64'), dekSealed: Buffer.from(v.dek_sealed_b64, 'base64'), kekId: v.kek_id },
      }
      if (v.opens) expect(openCredential(kek, row).secret).toBe(v.plaintext)
      else expect(() => openCredential(kek, row)).toThrow(LEGACY_FORMAT_MESSAGE)
    }
    for (const v of built.secrets) {
      const env = { secretSealed: Buffer.from(v.secret_sealed_b64, 'base64'), dekSealed: Buffer.from(v.dek_sealed_b64, 'base64'), kekId: v.kek_id }
      expect(openSecret(kek, env, v.aad)).toBe(v.plaintext)
    }
  })
})
