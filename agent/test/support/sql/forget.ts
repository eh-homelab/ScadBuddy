import type { Sql } from 'postgres'
import { BackgroundTaskStore } from '../../../src/background/store.js'
import type { ForgetDeps } from '../../../src/sessions/forget.js'
import type { PayloadKeys } from '../../../src/temporal/payloadCodec.js'
import { SqlBackgroundTasks } from './backgroundTasks.js'
import { payloadKeyLock } from './payloadKeys.js'
import { dropUnnamedBytes } from './sessionFiles.js'
import { PgSessionReads } from './sessionReads.js'
import { copyForkRanges } from './transcriptRows.js'

// forgetSubject's routes over SQL on the test database: what the API's `forget`, `forget-rows`,
// `background-tasks` and `session-reads` routes run (backend api/crud_agent_forget.py,
// tools/go/core/internal/sweeps), as the agent once ran them itself. Test-only.

/**
 * `forgetSubject`'s deps over `sql`, with `extra` on top. `extra.keys` opens and seals the
 * transcript a session's forks take (step 1); without it, a session whose forks take entries
 * is refused ForksNeedKeys, as an API with no KEK refuses it.
 */
export function sqlForget(
  sql: Sql,
  extra: Partial<ForgetDeps> & { keys?: (PayloadKeys & { forget(subject: string): void }) | undefined } = {},
): ForgetDeps {
  const keys = extra.keys
  return {
    crud: {
      forget: (subject) =>
        sql.begin(async (tx) => {
          await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
          if (subject.startsWith('session-')) await copyForkRanges(tx, keys, subject.slice('session-'.length))
          await tx`INSERT INTO ai_forgotten_subjects (subject) VALUES (${subject}) ON CONFLICT (subject) DO NOTHING`
          if (subject.startsWith('flow-')) await tx`DELETE FROM ai_flow_blobs WHERE subject = ${subject}`
          const deleted = await tx`DELETE FROM ai_payload_keys WHERE subject = ${subject}`
          return deleted.count > 0
        }),
    },
    tasks: new BackgroundTaskStore(new SqlBackgroundTasks(sql)),
    reads: new PgSessionReads(sql),
    rows: {
      forget: async (id) => {
        const shared = await sql<{ bytes_sha256: string }[]>`
          SELECT DISTINCT bytes_sha256 FROM ai_session_files WHERE session_id = ${id} AND bytes_sha256 IS NOT NULL`
        const entries = await sql`DELETE FROM ai_session_entries WHERE session_id = ${id}`
        const sessions = await sql`DELETE FROM ai_sessions WHERE id = ${id}`
        await dropUnnamedBytes(
          sql,
          shared.map((row) => row.bytes_sha256),
        ).catch(() => undefined)
        await sql`SELECT pg_notify('scadbuddy_warm_evict', ${id})`
        return entries.count + sessions.count
      },
    },
    ...extra,
  }
}
