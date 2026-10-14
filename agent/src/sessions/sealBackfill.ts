import type { Sql } from 'postgres'
import { sealBytes } from '../secrets.js'
import { type PayloadKeys, SubjectForgotten } from '../temporal/payloadCodec.js'
import { entryContext, entryTypeOf } from './store.js'

// Seals the transcript rows written before sealing (unified agent runtime D7, task 3.2):
// each plaintext `entry` becomes `sealed` under its session's data key with the AAD, and
// `entry_type` rule, that PostgresSessionStore.append uses, and `entry` is set NULL in the
// same UPDATE. A session whose subject was forgotten has no key and none may be made, so
// its leftover plaintext rows are deleted instead. Idempotent: a sealed row is never
// selected again.

/** `pg_advisory_xact_lock` key ("SCADSEAL" in ASCII); distinct from the migrations' "SCADAGNT". */
export const SEAL_LOCK = 0x5343_4144_5345_414cn

export const SEAL_BATCH = 500

type PlainRow = { id: string; session_id: string; subpath: string; uuid: string | null; entry: string }

/**
 * Seals every plaintext row, one transaction per batch, each under the lock, walking ids upward so a row
 * it skips is not read again; returns what it sealed, deleted and found malformed (counted in sealed too). A row whose text is not JSON
 * is sealed too, as opaque text (only its id is logged), and still fails in load as it did. The rows of a session with no `ai_sessions` row, or of a forgotten
 * one, are deleted, so no key is made for either.
 */
export async function sealBackfill(
  sql: Sql,
  keys: PayloadKeys,
  options: { batchSize?: number } = {},
): Promise<{ sealed: number; deleted: number; malformed: number }> {
  const batchSize = options.batchSize ?? SEAL_BATCH
  const total = { sealed: 0, deleted: 0, malformed: 0 }
  let after = '0'
  for (;;) {
    const done = await sql.begin(async (tx) => {
      // Taken before the read, so a run that waited sees what the one before it committed.
      await tx`SELECT pg_advisory_xact_lock(${SEAL_LOCK.toString()}::bigint)`
      const rows = await tx<PlainRow[]>`
        SELECT id::text AS id, session_id, subpath, uuid, entry FROM ai_session_entries
        WHERE sealed IS NULL AND entry IS NOT NULL AND id > ${after}::bigint ORDER BY ai_session_entries.id LIMIT ${batchSize}`
      if (rows.length === 0) return true
      after = rows[rows.length - 1]!.id
      const sessions = [...new Set(rows.map((row) => row.session_id))]
      const live = new Set(
        (await tx<{ id: string }[]>`SELECT id::text AS id FROM ai_sessions WHERE id::text IN ${tx(sessions)}`).map((row) => row.id),
      )
      for (const sessionId of sessions) {
        let dek: Buffer | undefined
        if (live.has(sessionId)) {
          try {
            dek = await keys.keyFor(`session-${sessionId}`, true)
          } catch (err) {
            if (!(err instanceof SubjectForgotten)) throw err
          }
        }
        if (!dek) {
          const gone = await tx`DELETE FROM ai_session_entries WHERE session_id = ${sessionId} AND sealed IS NULL AND entry IS NOT NULL`
          total.deleted += gone.count
          continue
        }
        try {
          for (const row of rows) {
            if (row.session_id !== sessionId) continue
            let type: string | null = null
            try {
              type = entryTypeOf(JSON.parse(row.entry) as { type?: unknown })
            } catch {
              // Sealed as it is, so load fails on it as it does on a malformed plaintext row: JSON.parse of what opens.
              console.error(`session transcripts: row ${row.id} is not JSON; sealed as it is`)
              total.malformed++
            }
            const sealed = sealBytes(dek, Buffer.from(row.entry, 'utf8'), entryContext(sessionId, row.subpath, row.uuid ?? `#${row.id}`))
            const updated = await tx`
              UPDATE ai_session_entries SET sealed = ${sealed}, entry = NULL, entry_type = ${type}
              WHERE id = ${row.id} AND sealed IS NULL`
            total.sealed += updated.count
          }
        } finally {
          dek.fill(0)
        }
      }
      return rows.length < batchSize
    })
    if (done) return total
  }
}
