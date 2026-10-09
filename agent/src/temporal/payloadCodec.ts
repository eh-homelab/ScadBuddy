import type { Payload, PayloadCodec, SerializationContext } from '@temporalio/common'
import { randomBytes } from 'node:crypto'
import type { Sql } from 'postgres'
import { KEK_BYTES, type Kek, openBytes, type RandomSource, SealError, sealBytes } from '../secrets.js'

// The payload codec of durable subjects (spec 2026-10-01 §6.5, plan 5c Ruling 11). Every
// payload of a workflow `session-<uuid>` or `flow-<uuid>` is sealed under that subject's
// own data key, a row of `ai_payload_keys`; deleting the row (forgetSubject) makes every
// copy of the subject's payloads undecryptable: history, Visibility, Archival. Other
// workflows' payloads (renders, prints, commands) pass through as they are.
//
// The subject comes from the serialization context on encode (the workflow the payload
// belongs to, `@temporalio/common` 1.24 gives it to every client and worker call), and
// from the payload's own metadata on decode, so a payload decodes with or without
// context. agent-durable's codec.py is the same codec in Python; the vectors in
// test/fixtures/payload-vectors.json pin the two together.
//
// An encoded payload: metadata `encoding: binary/scadbuddy-subject` and
// `scadbuddy-subject: <subject>`; data = the original payload as JSON
// (`{"d": <data b64>, "m": {<key>: <value b64>}}`, keys sorted, no spaces), sealed
// (secrets.ts format) under the subject's key in the context `payload:<subject>`. The
// key is 32 random bytes sealed under the KEK in the context `dek:ai_payload_keys:<subject>`.

export const SUBJECT_ENCODING = 'binary/scadbuddy-subject'
export const SUBJECT_METADATA = 'scadbuddy-subject'

const SUBJECT = /^(session|flow)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const encoder = new TextEncoder()
const decoder = new TextDecoder()

/** The subject a workflow ID names, or undefined for a workflow that carries no conversation. */
export function subjectOf(workflowId: string | undefined): string | undefined {
  return workflowId !== undefined && SUBJECT.test(workflowId) ? workflowId : undefined
}

/** The context a subject's data key is sealed in under the KEK. */
export const payloadKeyContext = (subject: string): string => `dek:ai_payload_keys:${subject}`
/** The context a payload is sealed in under its subject's key. */
export const payloadContext = (subject: string): string => `payload:${subject}`

/** The subject's payloads cannot be decoded: its key was deleted (forgetSubject). */
export class SubjectForgotten extends Error {
  override name = 'SubjectForgotten'
}

const b64 = (bytes: Uint8Array | null | undefined): string => Buffer.from(bytes ?? new Uint8Array()).toString('base64')

/** The original payload as the bytes that are sealed: sorted keys, so both languages agree. */
export function payloadBytes(payload: Payload): Buffer {
  const metadata = payload.metadata ?? {}
  const m: Record<string, string> = {}
  for (const key of Object.keys(metadata).sort()) m[key] = b64(metadata[key])
  return Buffer.from(JSON.stringify({ d: b64(payload.data), m }), 'utf8')
}

function payloadFrom(bytes: Buffer): Payload {
  const { d, m } = JSON.parse(bytes.toString('utf8')) as { d: string; m: Record<string, string> }
  const metadata: Record<string, Uint8Array> = {}
  for (const [key, value] of Object.entries(m)) metadata[key] = new Uint8Array(Buffer.from(value, 'base64'))
  return { metadata, data: new Uint8Array(Buffer.from(d, 'base64')) }
}

export function sealPayload(dek: Buffer, subject: string, payload: Payload, random?: RandomSource): Payload {
  const plain = payloadBytes(payload)
  try {
    return {
      metadata: { encoding: encoder.encode(SUBJECT_ENCODING), [SUBJECT_METADATA]: encoder.encode(subject) },
      data: new Uint8Array(sealBytes(dek, plain, payloadContext(subject), random)),
    }
  } finally {
    plain.fill(0)
  }
}

/** The subject an encoded payload belongs to; undefined for any other payload. */
export function sealedSubject(payload: Payload): string | undefined {
  const encoding = payload.metadata?.encoding
  if (!encoding || decoder.decode(encoding) !== SUBJECT_ENCODING) return undefined
  const subject = payload.metadata?.[SUBJECT_METADATA]
  return subject ? decoder.decode(subject) : ''
}

export function openPayload(dek: Buffer, subject: string, payload: Payload): Payload {
  const plain = openBytes(dek, Buffer.from(payload.data ?? new Uint8Array()), payloadContext(subject))
  try {
    return payloadFrom(plain)
  } finally {
    plain.fill(0)
  }
}

/** Where a subject's data key is found, and made when `create` and none exists. */
export interface PayloadKeys {
  /** The subject's data key; SubjectForgotten when there is none and `create` is false. */
  keyFor(subject: string, create: boolean): Promise<Buffer>
}

export type PgPayloadKeysOptions = {
  /** The key mounted as SCADBUDDY_SECRET_KEY_PREVIOUS_FILE, during a rotation. */
  previous?: Kek | undefined
  /** How long a data key is kept in memory (default 5 minutes). */
  cacheMs?: number
  /** At most this many keys in memory (default 1000). */
  cacheMax?: number
}

/** `ai_payload_keys`, sealed under the KEK; a key is cached in memory for minutes at most. */
export class PgPayloadKeys implements PayloadKeys {
  readonly #sql: Sql
  readonly #kek: Kek
  readonly #previous: Kek | undefined
  readonly #cacheMs: number
  readonly #cacheMax: number
  readonly #cache = new Map<string, { key: Buffer; until: number }>()

  constructor(sql: Sql, kek: Kek, options: PgPayloadKeysOptions = {}) {
    this.#sql = sql
    this.#kek = kek
    this.#previous = options.previous
    this.#cacheMs = options.cacheMs ?? 5 * 60_000
    this.#cacheMax = options.cacheMax ?? 1000
  }

  /** Drops a subject's cached key (forgetSubject, before it deletes the row). */
  forget(subject: string): void {
    this.#cache.get(subject)?.key.fill(0)
    this.#cache.delete(subject)
  }

  async keyFor(subject: string, create: boolean): Promise<Buffer> {
    const cached = this.#cache.get(subject)
    if (cached && cached.until > Date.now()) return cached.key
    if (cached) this.forget(subject)
    let [row] = await this.#sql<{ dek_sealed: Buffer; kek_id: string }[]>`
      SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = ${subject}`
    if (!row && create) {
      const dek = randomBytes(KEK_BYTES)
      try {
        const sealed = sealBytes(this.#kek.key, dek, payloadKeyContext(subject))
        await this.#sql`
          INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES (${subject}, ${sealed}, ${this.#kek.id})
          ON CONFLICT (subject) DO NOTHING`
      } finally {
        dek.fill(0)
      }
      // Whoever inserted first, this is the key every encoder of the subject uses.
      ;[row] = await this.#sql<{ dek_sealed: Buffer; kek_id: string }[]>`
        SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = ${subject}`
    }
    if (!row) throw new SubjectForgotten(`the payloads of ${subject} cannot be decoded: its key was deleted`)
    const kek = row.kek_id === this.#kek.id ? this.#kek : row.kek_id === this.#previous?.id ? this.#previous : undefined
    if (!kek) throw new SealError(`the payload key of ${subject} was sealed with key ${row.kek_id}, which is not mounted`)
    const key = openBytes(kek.key, row.dek_sealed, payloadKeyContext(subject))
    if (this.#cache.size >= this.#cacheMax) {
      const oldest = this.#cache.keys().next().value
      if (oldest !== undefined) this.forget(oldest)
    }
    this.#cache.set(subject, { key, until: Date.now() + this.#cacheMs })
    return key
  }
}

function workflowIdOf(context: SerializationContext | undefined): string | undefined {
  return context?.workflowId ?? undefined
}

export class SubjectPayloadCodec implements PayloadCodec {
  readonly #keys: PayloadKeys

  constructor(keys: PayloadKeys) {
    this.#keys = keys
  }

  async encode(payloads: Payload[], context?: SerializationContext): Promise<Payload[]> {
    const subject = subjectOf(workflowIdOf(context))
    if (subject === undefined) return payloads
    const key = await this.#keys.keyFor(subject, true)
    return payloads.map((p) => (sealedSubject(p) === undefined ? sealPayload(key, subject, p) : p))
  }

  async decode(payloads: Payload[]): Promise<Payload[]> {
    const out: Payload[] = []
    for (const p of payloads) {
      const subject = sealedSubject(p)
      if (subject === undefined) {
        out.push(p)
        continue
      }
      if (!SUBJECT.test(subject)) throw new SealError('a sealed payload names no subject')
      out.push(openPayload(await this.#keys.keyFor(subject, false), subject, p))
    }
    return out
  }
}

/** Re-wraps every payload key sealed under `previous` to `current` (key rotation, main.ts). */
export async function rewrapPayloadKeys(sql: Sql, previous: Kek, current: Kek): Promise<{ rewrapped: number; failed: number }> {
  const result = { rewrapped: 0, failed: 0 }
  const rows = await sql<{ subject: string; dek_sealed: Buffer }[]>`
    SELECT subject, dek_sealed FROM ai_payload_keys WHERE kek_id = ${previous.id}`
  for (const row of rows) {
    let dek: Buffer | undefined
    try {
      dek = openBytes(previous.key, row.dek_sealed, payloadKeyContext(row.subject))
      const sealed = sealBytes(current.key, dek, payloadKeyContext(row.subject))
      const updated = await sql`
        UPDATE ai_payload_keys SET dek_sealed = ${sealed}, kek_id = ${current.id}
        WHERE subject = ${row.subject} AND kek_id = ${previous.id}`
      result.rewrapped += updated.count
    } catch (err) {
      if (!(err instanceof SealError)) throw err
      result.failed += 1
    } finally {
      dek?.fill(0)
    }
  }
  return result
}
