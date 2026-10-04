// Shared secret and payload test vectors (#1056, spec §6.2). TypeScript is the
// source of truth for the sealed format; the Python port (agent-durable) opens
// these. Run `pnpm gen:vectors` after any change to src/secrets.ts or an AAD,
// and commit test/fixtures/secret-vectors.json.
import { createCipheriv } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { type CredentialKind, credentialAad } from '../src/credentials.js'
import { kekFromBase64, KEK_BYTES, sealBytes, sealSecret, SEAL_V1 } from '../src/secrets.js'

const IV_BYTES = 12
const GATEWAY_URL = 'https://gateway.example/v1'

export type SecretVector = {
  name: string
  version: number
  row_id: string
  kind: CredentialKind
  base_url: string | null
  aad: string
  secret_sealed_b64: string
  dek_sealed_b64: string
  kek_id: string
  plaintext: string
}

export type PayloadVector = {
  subject: string
  dek_b64: string
  dek_sealed_b64: string
  plaintext_b64: string
  sealed_b64: string
}

export type SecretVectorFile = {
  kek_base64: string
  kek_id: string
  vectors: SecretVector[]
  payload: PayloadVector[]
}

/** Data keys are 0x22 + index, IVs 0x33 + counter: every byte of a vector is reproducible. */
function deterministicRandom(): (n: number) => Buffer {
  let dekIndex = 0
  let ivCounter = 0
  return (n) => {
    if (n === KEK_BYTES) return Buffer.alloc(n, 0x22 + dekIndex++)
    if (n === IV_BYTES) return Buffer.alloc(n, 0x33 + ivCounter++)
    throw new Error(`no deterministic source for ${n} bytes`)
  }
}

/** `0x01 | iv | tag | ct` with the context as AAD. The format secrets.ts still opens and never writes. */
function sealV1ForTests(key: Buffer, plaintext: Buffer, context: string, iv: Buffer): Buffer {
  const cipher = createCipheriv('aes-256-gcm', key, iv)
  cipher.setAAD(Buffer.from(context, 'utf8'))
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return Buffer.concat([Buffer.from([SEAL_V1]), iv, cipher.getAuthTag(), ciphertext])
}

export function secretVectors(): SecretVectorFile {
  const kekBase64 = Buffer.alloc(KEK_BYTES, 0x11).toString('base64')
  const kek = kekFromBase64(kekBase64)
  const random = deterministicRandom()

  const cases: { kind: CredentialKind; id: string; baseUrl: string | null; plaintext: string }[] = [
    { kind: 'anthropic_api_key', id: 'default', baseUrl: null, plaintext: 'sk-ant-test-0001' },
    { kind: 'claude_oauth_token', id: 'c-2', baseUrl: null, plaintext: 'oauth-test-0002' },
    { kind: 'gateway', id: 'c-3', baseUrl: GATEWAY_URL, plaintext: 'gw-token-0003' },
  ]
  const vectors: SecretVector[] = cases.map((c) => {
    const aad = credentialAad(c.id, c.kind, c.baseUrl)
    const envelope = sealSecret(kek, c.plaintext, aad, random)
    return {
      name: `${c.kind}.v2`,
      version: 2,
      row_id: c.id,
      kind: c.kind,
      base_url: c.baseUrl,
      aad,
      secret_sealed_b64: envelope.secretSealed.toString('base64'),
      dek_sealed_b64: envelope.dekSealed.toString('base64'),
      kek_id: envelope.kekId,
      plaintext: c.plaintext,
    }
  })

  const v1Aad = 'ai_credentials:default'
  const v1Dek = Buffer.alloc(KEK_BYTES, 0x2f)
  const v1Plaintext = 'sk-ant-legacy-0004'
  vectors.push({
    name: 'v1',
    version: 1,
    row_id: 'default',
    kind: 'anthropic_api_key',
    base_url: null,
    aad: v1Aad,
    secret_sealed_b64: sealV1ForTests(v1Dek, Buffer.from(v1Plaintext, 'utf8'), v1Aad, Buffer.alloc(IV_BYTES, 0x3f)).toString('base64'),
    dek_sealed_b64: sealV1ForTests(kek.key, v1Dek, `dek:${v1Aad}`, Buffer.alloc(IV_BYTES, 0x3e)).toString('base64'),
    kek_id: kek.id,
    plaintext: v1Plaintext,
  })

  // Plan ruling 11: the data key is sealed under the KEK with `dek:ai_payload_keys:<subject>`,
  // the payload under the data key with `ai_payload:<subject>`.
  const payload: PayloadVector[] = [
    { subject: 'session-00000000-0000-4000-8000-000000000001', text: '{"turn":1,"text":"hello"}' },
    { subject: 'flow-00000000-0000-4000-8000-000000000002', text: '{"flow":"x","ok":true}' },
  ].map((p, i) => {
    const dek = Buffer.alloc(KEK_BYTES, 0x44 + i)
    const plaintext = Buffer.from(p.text, 'utf8')
    return {
      subject: p.subject,
      dek_b64: dek.toString('base64'),
      dek_sealed_b64: sealBytes(kek.key, dek, `dek:ai_payload_keys:${p.subject}`, random).toString('base64'),
      plaintext_b64: plaintext.toString('base64'),
      sealed_b64: sealBytes(dek, plaintext, `ai_payload:${p.subject}`, random).toString('base64'),
    }
  })

  return { kek_base64: kekBase64, kek_id: kek.id, vectors, payload }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  // Run from agent/ (pnpm gen:vectors), so the path is relative to it.
  writeFileSync('test/fixtures/secret-vectors.json', `${JSON.stringify(secretVectors(), null, 2)}\n`)
}
