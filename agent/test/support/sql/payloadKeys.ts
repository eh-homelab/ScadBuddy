import { randomBytes } from 'node:crypto'
import type { Sql } from 'postgres'
import { KEK_BYTES, type Kek, openBytes, SealError, sealBytes } from '../../../src/secrets.js'
import {
  type ForgetsKeys,
  KeyCache,
  type KeyForOptions,
  type PayloadKeys,
  payloadKeyContext,
  SubjectForgotten,
} from '../../../src/temporal/payloadCodec.js'

// The API's payload keys (tools/go/core/internal/secrets, agent_data/), over SQL on the test
// database: what the agent ran itself before #5299 step 12. Test-only.

/** The advisory lock key creation and forgetSubject take for a subject (Ruling 11). */
export const payloadKeyLock = (subject: string): string => `scadbuddy:ai_payload_keys:${subject}`

export type PgPayloadKeysOptions = {
  /** The key mounted as SCADBUDDY_SECRET_KEY_PREVIOUS_FILE, during a rotation. */
  previous?: Kek | undefined
  /** How long a data key is kept in memory (default 30 s): how long another process can still open a forgotten subject. */
  cacheMs?: number
  /** At most this many keys in memory (default 1000). */
  cacheMax?: number
}

/** `ai_payload_keys`, sealed under `kek`; a key is cached in memory for 30 s at most. */
export class PgPayloadKeys implements PayloadKeys, ForgetsKeys {
  readonly #sql: Sql
  readonly #kek: Kek
  readonly #previous: Kek | undefined
  readonly #cache: KeyCache

  constructor(sql: Sql, kek: Kek, options: PgPayloadKeysOptions = {}) {
    this.#sql = sql
    this.#kek = kek
    this.#previous = options.previous
    this.#cache = new KeyCache(options.cacheMs ?? 30_000, options.cacheMax ?? 1000)
  }

  /** Drops a subject's cached key (forgetSubject, before it deletes the row). */
  forget(subject: string): void {
    this.#cache.forget(subject)
  }

  async keyFor(subject: string, create: boolean, options: KeyForOptions = {}): Promise<Buffer> {
    const standing = options.onlyForStandingSession === true
    const cached = this.#cache.get(subject)
    if (cached) return cached
    let [row] = await this.#sql<{ dek_sealed: Buffer; kek_id: string }[]>`
      SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = ${subject}`
    if (!row && create) {
      const dek = randomBytes(KEK_BYTES)
      try {
        const sealed = sealBytes(this.#kek.key, dek, payloadKeyContext(subject))
        // Never for a forgotten subject: its payloads stay unreadable, new ones included.
        // Under the subject's lock, which forgetSubject also takes, so the tombstone
        // check never reads from before a forget that is committing.
        await this.#sql.begin(async (tx) => {
          await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
          await tx`
            INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id)
            SELECT ${subject}, ${sealed}, ${this.#kek.id}
            WHERE NOT EXISTS (SELECT 1 FROM ai_forgotten_subjects WHERE subject = ${subject})
              AND (NOT ${standing}::boolean OR EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = substr(${subject}, 9)))
            ON CONFLICT (subject) DO NOTHING`
        })
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
    return this.#cache.keep(subject, openBytes(kek.key, row.dek_sealed, payloadKeyContext(subject)))
  }
}

/** How old a browser call's key is before `sweepBridgeKeys` deletes it: far past the longest call. */
export const BRIDGE_KEY_MAX_AGE_S = 3600

/**
 * Deletes a subject's key, which crypto-shreds every payload sealed under it: a browser
 * call's (`bridge-<uuid>`) once its caller has the answer. No tombstone: nothing takes the
 * id again. `keys` drops this process's cached copy first.
 */
export async function dropPayloadKey(sql: Sql, keys: ForgetsKeys | undefined, subject: string): Promise<void> {
  keys?.forget(subject)
  await sql.begin(async (tx) => {
    await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
    await tx`DELETE FROM ai_payload_keys WHERE subject = ${subject}`
  })
}

/** Deletes the keys of browser calls older than `maxAgeS` (one cancelled, or whose caller went away), and returns how many. */
export async function sweepBridgeKeys(sql: Sql, maxAgeS = BRIDGE_KEY_MAX_AGE_S): Promise<number> {
  const gone = await sql`
    DELETE FROM ai_payload_keys
    WHERE subject LIKE 'bridge-%' AND created_at < now() - make_interval(secs => ${maxAgeS})`
  return gone.count
}

/** Re-wraps every payload key sealed under `previous` to `current`, as the API does at a rotation (backend `agent_data/rotation.py`). */
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
