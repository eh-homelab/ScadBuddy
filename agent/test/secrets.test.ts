import { randomBytes } from 'node:crypto'
import { mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  type Envelope,
  kekFromBase64,
  last4,
  loadKek,
  openSecret,
  redact,
  rewrap,
  SealError,
  sealSecret,
  SecretKeyError,
} from '../src/secrets.js'

const newKek = () => kekFromBase64(randomBytes(32).toString('base64'))
const AAD = 'ai_credentials:default'
const SECRET = 'sk-ant-api03-this-is-a-test-secret-9f3a'

function flip(buf: Buffer, index: number): Buffer {
  const copy = Buffer.from(buf)
  copy[index] = (copy[index] ?? 0) ^ 0x01
  return copy
}

describe('envelope encryption', () => {
  it('round-trips a secret', () => {
    const kek = newKek()
    const envelope = sealSecret(kek, SECRET, AAD)
    expect(openSecret(kek, envelope, AAD)).toBe(SECRET)
  })

  it('never stores the plaintext and uses a fresh data key and IV each time', () => {
    const kek = newKek()
    const a = sealSecret(kek, SECRET, AAD)
    const b = sealSecret(kek, SECRET, AAD)
    for (const sealed of [a.secretSealed, a.dekSealed]) {
      expect(sealed.includes(Buffer.from(SECRET))).toBe(false)
    }
    expect(a.secretSealed.equals(b.secretSealed)).toBe(false)
    expect(a.dekSealed.equals(b.dekSealed)).toBe(false)
    expect(a.kekId).toBe(kek.id)
  })

  it.each([
    ['the version byte', 'secretSealed', 0],
    ['the IV', 'secretSealed', 3],
    ['the tag', 'secretSealed', 20],
    ['the ciphertext', 'secretSealed', 40],
    ['the sealed data key', 'dekSealed', 45],
  ] as const)('detects tampering with %s', (_name, field, index) => {
    const kek = newKek()
    const envelope = sealSecret(kek, SECRET, AAD)
    const tampered: Envelope = { ...envelope, [field]: flip(envelope[field], index) }
    expect(() => openSecret(kek, tampered, AAD)).toThrow(SealError)
  })

  it('refuses a truncated value', () => {
    const kek = newKek()
    const envelope = sealSecret(kek, SECRET, AAD)
    expect(() => openSecret(kek, { ...envelope, secretSealed: envelope.secretSealed.subarray(0, 20) }, AAD)).toThrow(
      SealError,
    )
  })

  it('fails with the wrong KEK, naming both key ids', () => {
    const envelope = sealSecret(newKek(), SECRET, AAD)
    const other = newKek()
    expect(() => openSecret(other, envelope, AAD)).toThrow(/sealed with key [0-9a-f]{16}.*holds key [0-9a-f]{16}/)
    // Even with a forged key id, the wrong key cannot open the data key.
    expect(() => openSecret(other, { ...envelope, kekId: other.id }, AAD)).toThrow(SealError)
  })

  it('binds the value to its row: another AAD fails', () => {
    const kek = newKek()
    const envelope = sealSecret(kek, SECRET, AAD)
    expect(() => openSecret(kek, envelope, 'ai_credentials:other')).toThrow(SealError)
  })

  it('rotates by re-wrapping the data key only', () => {
    const oldKek = newKek()
    const nextKek = newKek()
    const envelope = sealSecret(oldKek, SECRET, AAD)
    const rotated = rewrap(oldKek, nextKek, envelope, AAD)
    expect(rotated.secretSealed.equals(envelope.secretSealed)).toBe(true)
    expect(rotated.kekId).toBe(nextKek.id)
    expect(openSecret(nextKek, rotated, AAD)).toBe(SECRET)
    expect(() => openSecret(oldKek, rotated, AAD)).toThrow(SealError)
  })

  it('error messages never contain the secret', () => {
    const kek = newKek()
    const envelope = sealSecret(kek, SECRET, AAD)
    try {
      openSecret(kek, { ...envelope, secretSealed: flip(envelope.secretSealed, 40) }, AAD)
    } catch (err) {
      expect(String(err)).not.toContain(SECRET)
    }
  })
})

describe('the key file', () => {
  it('accepts 32 bytes of base64 with surrounding whitespace', () => {
    const text = `${randomBytes(32).toString('base64')}\n`
    expect(kekFromBase64(text).key).toHaveLength(32)
  })

  it.each([
    ['a short key', randomBytes(16).toString('base64')],
    ['a long key', randomBytes(48).toString('base64')],
    ['hex', randomBytes(32).toString('hex')],
    ['a passphrase', 'correct horse battery staple'],
    ['nothing', ''],
  ])('refuses %s', (_name, text) => {
    expect(() => kekFromBase64(text)).toThrow(SecretKeyError)
  })

  it('reports why it is unusable without throwing', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'kek-'))
    expect(await loadKek(undefined)).toEqual({ ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE is not set' })

    const missing = await loadKek(path.join(dir, 'missing.key'))
    expect(missing.ok).toBe(false)
    expect(!missing.ok && missing.reason).toMatch(/cannot be read \(ENOENT\)/)

    const bad = path.join(dir, 'bad.key')
    await writeFile(bad, 'not a key')
    const badStatus = await loadKek(bad)
    expect(!badStatus.ok && badStatus.reason).toMatch(/not base64/)

    const good = path.join(dir, 'good.key')
    await writeFile(good, randomBytes(32).toString('base64') + '\n')
    const goodStatus = await loadKek(good)
    expect(goodStatus.ok).toBe(true)
  })
})

describe('what may be shown of a secret', () => {
  it('shows the last four characters of a long secret only', () => {
    expect(last4(SECRET)).toBe('9f3a')
    expect(last4('short-key')).toBe('')
  })

  it('redacts every occurrence', () => {
    expect(redact(`401 for ${SECRET}; again ${SECRET}`, [SECRET, undefined])).toBe(
      '401 for [redacted]; again [redacted]',
    )
  })
})
