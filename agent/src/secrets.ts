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
//     version (1 byte) | IV (12 bytes) | GCM tag (16 bytes) | ciphertext
//
// with an additional-authenticated-data context that names where the value
// lives and what it is bound to (for the Claude credential: its row, kind and
// base URL, src/credentials.ts), so a ciphertext copied into another row, or a
// row whose bound columns were edited, fails authentication instead of
// decrypting. The KEK's id (the first 16 hex characters of SHA-256 over the
// key) is stored beside the row, which lets "wrong key" be reported as such
// and finds the rows to re-wrap.
//
// VERSIONS.
//   0x01 (#354): the AAD is the context string as given. Still opened, never
//        written: v1 leaves the version byte outside the authenticated data.
//   0x02: the AAD is `v2|` + the context, so the version byte is authenticated
//        too. Any later version must fold its byte in the same way (`aadFor`).
//        Everything is written as v2.
//
// PLAINTEXT IN MEMORY. The Buffers this module creates (the data key, the
// decrypted bytes, the UTF-8 encoding of a plaintext) are zeroed once they are
// no longer needed. That cannot extend to JS strings: a secret passed in or
// returned as a `string` is immutable, may be copied by the engine, and stays
// in the heap until the garbage collector reclaims it. Zeroing the Buffers
// shortens the exposure; it does not remove it.

export const KEK_BYTES = 32
const IV_BYTES = 12
const TAG_BYTES = 16
/** Opened only; see VERSIONS above. */
export const SEAL_V1 = 0x01
/** What `seal` writes. */
export const SEAL_VERSION = 0x02
const KNOWN_VERSIONS: ReadonlySet<number> = new Set([SEAL_V1, SEAL_VERSION])

/** The authenticated data for a value of `version` sealed in `context`. */
function aadFor(version: number, context: string): Buffer {
  // v1 did not authenticate its version byte; unchanged so v1 values still open.
  return Buffer.from(version === SEAL_V1 ? context : `v${version}|${context}`, 'utf8')
}

/** The format version byte of a sealed value, or undefined for an empty one. */
export function sealedVersion(sealed: Buffer): number | undefined {
  return sealed.length > 0 ? sealed[0] : undefined
}

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

/**
 * `reason` is safe to show unauthenticated (/healthz, GET credentials): it names
 * the variable, never the file path or the errno. `detail`, when present, is
 * for the service's own log only.
 */
export type KekStatus = { ok: true; kek: Kek } | { ok: false; reason: string; detail?: string }

/** Reads a key file (SCADBUDDY_SECRET_KEY_FILE unless `variable` says otherwise). Never throws. */
export async function loadKek(file: string | undefined, variable = 'SCADBUDDY_SECRET_KEY_FILE'): Promise<KekStatus> {
  if (file === undefined) {
    return { ok: false, reason: `${variable} is not set` }
  }
  let text: string
  try {
    text = await readFile(file, 'utf8')
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code ?? 'read error'
    return { ok: false, reason: `${variable} cannot be read`, detail: `${variable} ${file} cannot be read (${code})` }
  }
  try {
    return { ok: true, kek: kekFromBase64(text) }
  } catch (err) {
    return {
      ok: false,
      reason: `${variable} does not hold a valid key (32 random bytes, base64)`,
      detail: `${variable} ${file}: ${(err as Error).message}`,
    }
  }
}

/** Source of random bytes; tests and the vector generator inject a deterministic one. */
export type RandomBytes = (n: number) => Buffer

/**
 * Seals under `key` in the current version. Exported for the payload codec
 * (agent-durable's session payloads are sealed with the same format); `random`
 * defaults to the system CSPRNG and is injectable only for test vectors.
 */
function seal(key: Buffer, plaintext: Buffer, context: string, random: RandomBytes = randomBytes): Buffer {
  const iv = random(IV_BYTES)
  const cipher = createCipheriv('aes-256-gcm', key, iv)
  cipher.setAAD(aadFor(SEAL_VERSION, context))
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()])
  return Buffer.concat([Buffer.from([SEAL_VERSION]), iv, cipher.getAuthTag(), ciphertext])
}

/** Decrypts. The caller owns the returned Buffer and zeroes it. Exported for the payload codec. */
function open(key: Buffer, sealed: Buffer, context: string): Buffer {
  const version = sealedVersion(sealed)
  if (sealed.length < 1 + IV_BYTES + TAG_BYTES || version === undefined || !KNOWN_VERSIONS.has(version)) {
    throw new SealError('sealed value is malformed or of an unknown version')
  }
  const iv = sealed.subarray(1, 1 + IV_BYTES)
  const tag = sealed.subarray(1 + IV_BYTES, 1 + IV_BYTES + TAG_BYTES)
  const ciphertext = sealed.subarray(1 + IV_BYTES + TAG_BYTES)
  const decipher = createDecipheriv('aes-256-gcm', key, iv)
  decipher.setAAD(aadFor(version, context))
  decipher.setAuthTag(tag)
  // GCM releases unauthenticated plaintext from update(); it is zeroed below
  // whether or not final() then authenticates it.
  const head = decipher.update(ciphertext)
  let tail: Buffer
  try {
    tail = decipher.final()
  } catch {
    head.fill(0)
    throw new SealError('sealed value failed authentication (wrong key, or altered)')
  }
  const out = Buffer.concat([head, tail])
  head.fill(0)
  tail.fill(0)
  return out
}

export const sealBytes = seal
export const openBytes = open

/** What a row stores for one secret. */
export type Envelope = {
  /** The secret, sealed under the data key. */
  secretSealed: Buffer
  /** The per-row data key, sealed under the KEK. */
  dekSealed: Buffer
  /** Which KEK sealed `dekSealed` (see `Kek.id`). */
  kekId: string
}

export function sealSecret(kek: Kek, plaintext: string, aad: string, random: RandomBytes = randomBytes): Envelope {
  const dek = random(KEK_BYTES)
  const bytes = Buffer.from(plaintext, 'utf8')
  try {
    return {
      secretSealed: seal(dek, bytes, aad, random),
      dekSealed: seal(kek.key, dek, `dek:${aad}`, random),
      kekId: kek.id,
    }
  } finally {
    dek.fill(0)
    bytes.fill(0)
  }
}

export function openSecret(kek: Kek, envelope: Envelope, aad: string): string {
  if (envelope.kekId !== kek.id) {
    throw new SealError(
      `secret was sealed with key ${envelope.kekId}, but SCADBUDDY_SECRET_KEY_FILE holds key ${kek.id}`,
    )
  }
  const dek = open(kek.key, envelope.dekSealed, `dek:${aad}`)
  let bytes: Buffer | undefined
  try {
    bytes = open(dek, envelope.secretSealed, aad)
    return bytes.toString('utf8')
  } finally {
    dek.fill(0)
    bytes?.fill(0)
  }
}

/**
 * Key rotation: re-wraps the data key under a new KEK; the sealed secret is
 * untouched (spec §9, "Rotating it re-wraps the data keys only"). The new
 * wrapping is written in the current version.
 */
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
