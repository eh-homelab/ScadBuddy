import { createCipheriv, createDecipheriv, createHash, randomBytes } from 'node:crypto'
import { readFile } from 'node:fs/promises'

// Envelope encryption for secrets at rest (design spec §9,
// docs/superpowers/specs/2026-09-27-ai-integration-design.md):
//
//   "Each secret is sealed with AES-256-GCM under a random per-row data key.
//    The data key is sealed under a key-encryption key read from a file
//    (SCADBUDDY_SECRET_KEY_FILE, mounted from a Kubernetes Secret ...).
//    Rotating it re-wraps the data keys only. Without the file, Settings
//    refuses to save credentials and says why."
//
// KEY FILE FORMAT. Exactly 32 random bytes, base64-encoded (standard alphabet,
// with padding: 44 characters). Surrounding whitespace, including the trailing
// newline most tools write, is ignored. Generate one with
//
//     openssl rand -base64 32 > scadbuddy-secret.key
//
// Anything else (hex, a passphrase, a different length) is refused at load
// rather than stretched into a key, so a mis-mounted file is visible.
//
// SEALED FORMAT. Both the secret and its data key are stored as
//
//     version (1 byte, 0x01) | IV (12 bytes) | GCM tag (16 bytes) | ciphertext
//
// with an additional-authenticated-data string that names where the value
// lives (e.g. `ai_credentials:default`), so a ciphertext copied into another
// row fails authentication instead of decrypting there. The KEK's id (the
// first 16 hex characters of SHA-256 over the key) is stored beside the row,
// which lets "wrong key" be reported as such and finds the rows to re-wrap.

export const KEK_BYTES = 32
const IV_BYTES = 12
const TAG_BYTES = 16
const VERSION = 0x01

export class SecretKeyError extends Error {
  override name = 'SecretKeyError'
}

/** Decryption failed: wrong key, or the stored bytes were altered. Never carries plaintext. */
export class SealError extends Error {
  override name = 'SealError'
}

/** A loaded key-encryption key. The raw bytes never leave this module's functions. */
export type Kek = { readonly id: string; readonly key: Buffer }

export function kekFromBase64(text: string): Kek {
  const trimmed = text.trim()
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(trimmed) || trimmed.length % 4 !== 0) {
    throw new SecretKeyError(
      'the key file is not base64; it must hold 32 random bytes, base64-encoded (openssl rand -base64 32)',
    )
  }
  const key = Buffer.from(trimmed, 'base64')
  if (key.length !== KEK_BYTES) {
    throw new SecretKeyError(
      `the key file decodes to ${key.length} bytes, not ${KEK_BYTES} (openssl rand -base64 32)`,
    )
  }
  return { id: createHash('sha256').update(key).digest('hex').slice(0, 16), key }
}

export type KekStatus = { ok: true; kek: Kek } | { ok: false; reason: string }

/** Reads SCADBUDDY_SECRET_KEY_FILE. Never throws: the reason is reported by /healthz and the API. */
export async function loadKek(file: string | undefined): Promise<KekStatus> {
  if (file === undefined) {
    return { ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE is not set' }
  }
  let text: string
  try {
    text = await readFile(file, 'utf8')
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code ?? 'read error'
    return { ok: false, reason: `SCADBUDDY_SECRET_KEY_FILE ${file} cannot be read (${code})` }
  }
  try {
    return { ok: true, kek: kekFromBase64(text) }
  } catch (err) {
    return { ok: false, reason: `SCADBUDDY_SECRET_KEY_FILE ${file}: ${(err as Error).message}` }
  }
}

function seal(key: Buffer, plaintext: Buffer, aad: string): Buffer {
  const iv = randomBytes(IV_BYTES)
  const cipher = createCipheriv('aes-256-gcm', key, iv)
  cipher.setAAD(Buffer.from(aad, 'utf8'))
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return Buffer.concat([Buffer.from([VERSION]), iv, cipher.getAuthTag(), ciphertext])
}

function open(key: Buffer, sealed: Buffer, aad: string): Buffer {
  if (sealed.length < 1 + IV_BYTES + TAG_BYTES || sealed[0] !== VERSION) {
    throw new SealError('sealed value is malformed or of an unknown version')
  }
  const iv = sealed.subarray(1, 1 + IV_BYTES)
  const tag = sealed.subarray(1 + IV_BYTES, 1 + IV_BYTES + TAG_BYTES)
  const ciphertext = sealed.subarray(1 + IV_BYTES + TAG_BYTES)
  const decipher = createDecipheriv('aes-256-gcm', key, iv)
  decipher.setAAD(Buffer.from(aad, 'utf8'))
  decipher.setAuthTag(tag)
  try {
    return Buffer.concat([decipher.update(ciphertext), decipher.final()])
  } catch {
    throw new SealError('sealed value failed authentication (wrong key, or altered)')
  }
}

/** What a row stores for one secret. */
export type Envelope = {
  /** The secret, sealed under the data key. */
  secretSealed: Buffer
  /** The per-row data key, sealed under the KEK. */
  dekSealed: Buffer
  /** Which KEK sealed `dekSealed` (see `Kek.id`). */
  kekId: string
}

export function sealSecret(kek: Kek, plaintext: string, aad: string): Envelope {
  const dek = randomBytes(KEK_BYTES)
  try {
    return {
      secretSealed: seal(dek, Buffer.from(plaintext, 'utf8'), aad),
      dekSealed: seal(kek.key, dek, `dek:${aad}`),
      kekId: kek.id,
    }
  } finally {
    dek.fill(0)
  }
}

export function openSecret(kek: Kek, envelope: Envelope, aad: string): string {
  if (envelope.kekId !== kek.id) {
    throw new SealError(
      `secret was sealed with key ${envelope.kekId}, but SCADBUDDY_SECRET_KEY_FILE holds key ${kek.id}`,
    )
  }
  const dek = open(kek.key, envelope.dekSealed, `dek:${aad}`)
  try {
    return open(dek, envelope.secretSealed, aad).toString('utf8')
  } finally {
    dek.fill(0)
  }
}

/** Key rotation: re-wraps the data key under a new KEK; the sealed secret is untouched. */
export function rewrap(oldKek: Kek, newKek: Kek, envelope: Envelope, aad: string): Envelope {
  if (envelope.kekId !== oldKek.id) {
    throw new SealError(`secret was sealed with key ${envelope.kekId}, not ${oldKek.id}`)
  }
  const dek = open(oldKek.key, envelope.dekSealed, `dek:${aad}`)
  try {
    return { secretSealed: envelope.secretSealed, dekSealed: seal(newKek.key, dek, `dek:${aad}`), kekId: newKek.id }
  } finally {
    dek.fill(0)
  }
}

/**
 * The last four characters, the only part of a secret any route returns. Empty
 * for a secret shorter than 12 characters, where four would be a third of it.
 */
export function last4(secret: string): string {
  return secret.length < 12 ? '' : secret.slice(-4)
}

/** Replaces every occurrence of each secret in `text`; for error messages and logs. */
export function redact(text: string, secrets: readonly (string | undefined)[]): string {
  let out = text
  for (const secret of secrets) {
    if (secret && secret.length >= 4) out = out.split(secret).join('[redacted]')
  }
  return out
}
