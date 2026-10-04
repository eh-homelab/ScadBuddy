import { randomBytes } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import { KEK_BYTES, type Kek, SealError } from '../secrets.js'
import { isSubject, unwrapDataKey, wrapDataKey } from './codec.js'

// The payload codec's data keys (spec 2026-10-01 §6.5, plan ruling 11): one row of
// ai_payload_keys per subject, its data key sealed under the KEK with AAD
// `dek:ai_payload_keys:<subject>`. A key is cached for 60 s, so a subject forgotten
// by any process stops decrypting everywhere within a minute. agent-durable's
// scadbuddy_durable/payload_keys.py reads the same rows.

export const PAYLOAD_KEY_CACHE_MS = 60_000

export interface PayloadKeys {
  /** Keys a subject; a no-op if it already has a key. `tx` puts the row in the caller's transaction. */
  createKey(subject: string, tx?: TransactionSql): Promise<void>
  /** The subject's data key, or undefined once it is gone. */
  dataKey(subject: string): Promise<Buffer | undefined>
  /** Deletes the subject's key: every copy of its payloads becomes unreadable. */
  forget(subject: string, tx?: TransactionSql): Promise<void>
}

export type PayloadKeks = { current: Kek; previous?: Kek }

export class PgPayloadKeys implements PayloadKeys {
  readonly #cache = new Map<string, { key: Buffer; until: number }>()
  readonly #now: () => number
  readonly #sql: Sql
  readonly keks: PayloadKeks

  constructor(sql: Sql, keks: PayloadKeks, options: { now?: () => number } = {}) {
    this.#sql = sql
    this.keks = keks
    this.#now = options.now ?? Date.now
  }

  async createKey(subject: string, tx?: TransactionSql): Promise<void> {
    if (!isSubject(subject)) throw new Error(`${subject} is not a session or flow workflow id`)
    const dek = randomBytes(KEK_BYTES)
    try {
      const sealed = wrapDataKey(this.keks.current.key, subject, dek)
      await (tx ?? this.#sql)`
        INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id)
        VALUES (${subject}, ${sealed}, ${this.keks.current.id})
        ON CONFLICT (subject) DO NOTHING`
    } finally {
      dek.fill(0)
    }
  }

  async dataKey(subject: string): Promise<Buffer | undefined> {
    const cached = this.#cache.get(subject)
    if (cached && cached.until > this.#now()) return cached.key
    this.#evict(subject)
    const [row] = await this.#sql<{ dek_sealed: Buffer; kek_id: string }[]>`
      SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = ${subject}`
    if (!row) return undefined
    const kek = [this.keks.current, this.keks.previous].find((k) => k?.id === row.kek_id)
    if (!kek) {
      throw new SealError(`payload key for ${subject} was sealed with key ${row.kek_id}, which is not configured`)
    }
    const key = unwrapDataKey(kek.key, subject, row.dek_sealed)
    this.#cache.set(subject, { key, until: this.#now() + PAYLOAD_KEY_CACHE_MS })
    return key
  }

  /**
   * Key rotation (spec §9): re-wraps every data key sealed under `previous` under
   * `current`. The data keys, and so every sealed payload, are unchanged. A row that
   * does not open with `previous` counts as failed and is left as it is.
   */
  async rewrapFrom(previous: Kek, current: Kek): Promise<{ rewrapped: number; failed: number }> {
    const result = { rewrapped: 0, failed: 0 }
    if (previous.id === current.id) return result
    const rows = await this.#sql<{ subject: string; dek_sealed: Buffer }[]>`
      SELECT subject, dek_sealed FROM ai_payload_keys WHERE kek_id = ${previous.id}`
    for (const row of rows) {
      let dek: Buffer
      try {
        dek = unwrapDataKey(previous.key, row.subject, row.dek_sealed)
      } catch (err) {
        if (err instanceof SealError) {
          result.failed++
          continue
        }
        throw err
      }
      try {
        const sealed = wrapDataKey(current.key, row.subject, dek)
        const updated = await this.#sql`
          UPDATE ai_payload_keys SET dek_sealed = ${sealed}, kek_id = ${current.id}
          WHERE subject = ${row.subject} AND kek_id = ${previous.id}`
        result.rewrapped += updated.count
      } finally {
        dek.fill(0)
      }
    }
    return result
  }

  async forget(subject: string, tx?: TransactionSql): Promise<void> {
    await (tx ?? this.#sql)`DELETE FROM ai_payload_keys WHERE subject = ${subject}`
    this.#evict(subject)
  }

  // Not zeroed: a codec call may still be sealing or opening with it.
  #evict(subject: string): void {
    this.#cache.delete(subject)
  }
}
