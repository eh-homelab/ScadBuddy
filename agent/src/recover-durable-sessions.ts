import { Client, Connection } from '@temporalio/client'
import { loadConfig } from './config.js'
import { connectDatabase } from './db.js'
import { loadKek } from './secrets.js'
import { decodeWith, durableRows, type RecoverLine, recoverDurableSession } from './sessions/recoverDurable.js'
import { PostgresSessionStore } from './sessions/store.js'
import { FilePayloadStore, PgPayloadKeys, SubjectPayloadCodec } from './temporal/payloadCodec.js'

// `node dist/recover-durable-sessions.js [--dry-run] [<session id>...]` (#4772): the
// conversation of each DurableSession that closed before plan Step 7.2, rebuilt from its closed
// run's history (retained, else from the namespace's archive) and appended to ai_session_entries,
// and its row set `runtime = unified`, so its next send resumes with its old turns
// (sessions/recoverDurable.ts). With no ids, every row still `runtime = durable`. Run it from one
// agent pod with the service's configuration: it needs Postgres, Temporal, the KEK and
// SCADBUDDY_PAYLOAD_DIR (the run's payloads are sealed under each session's data key, and its
// large ones are files there). Prints one JSON line per session and a summary; never a payload.
//
// `--dry-run` writes nothing: it reads each run's history and reports what it would write. The
// exit is 1 when a session failed or its row stays `durable` for a reason to look at
// (`no_history`, `inconsistent`); `empty` (no model segment ever committed) is not one.

const DRY_RUN = '--dry-run'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const args = process.argv.slice(2)
const dryRun = args.includes(DRY_RUN)
const ids = args.filter((a) => a !== DRY_RUN)
if (ids.some((id) => !UUID.test(id))) {
  console.error('usage: recover-durable-sessions [--dry-run] [<session id>...]')
  process.exit(2)
}
const config = loadConfig()
if (!config.databaseUrl || !config.temporalAddress) {
  console.error('recover-durable-sessions: SCADBUDDY_DATABASE_URL and SCADBUDDY_TEMPORAL_ADDRESS must be set')
  process.exit(2)
}
if (!config.payloadDir) {
  console.error('recover-durable-sessions: SCADBUDDY_PAYLOAD_DIR must be set: a run keeps its large payloads there')
  process.exit(2)
}
const kek = await loadKek(config.secretKeyFile)
if (!kek.ok) {
  console.error(`recover-durable-sessions: the secret key is needed (${kek.reason})`)
  process.exit(2)
}
const database = connectDatabase(config.databaseUrl)
if (!(await database.ready())) {
  console.error('recover-durable-sessions: the database is not reachable')
  process.exit(1)
}
const sql = database.sql
const previous = config.previousSecretKeyFile ? await loadKek(config.previousSecretKeyFile, 'SCADBUDDY_SECRET_KEY_PREVIOUS_FILE') : undefined
const keys = new PgPayloadKeys(sql, kek.kek, { previous: previous?.ok ? previous.kek : undefined })
const connection = await Connection.connect({ address: config.temporalAddress })
let failed = 0
try {
  // No data converter: the history is read raw and opened here, payload by payload.
  const client = new Client({ connection, namespace: config.temporalNamespace })
  const deps = {
    client,
    sql,
    decode: decodeWith(new SubjectPayloadCodec(keys, new FilePayloadStore(config.payloadDir))),
    store: new PostgresSessionStore(sql, { keys }),
  }
  const counts: Record<string, number> = {}
  for (const id of ids.length > 0 ? ids : await durableRows(sql)) {
    let line: RecoverLine | { session: string; error: string }
    try {
      line = await recoverDurableSession(deps, id, { dryRun })
    } catch (err) {
      failed++
      // The error's name and text: no payload is in either.
      line = { session: id, error: err instanceof Error ? `${err.name}: ${err.message}`.slice(0, 300) : 'error' }
    }
    const key = 'error' in line ? 'error' : line.outcome
    if (key === 'no_history' || key === 'inconsistent') failed++
    counts[key] = (counts[key] ?? 0) + 1
    console.log(JSON.stringify(line))
  }
  const [left] = await sql<{ n: number }[]>`SELECT count(*)::int AS n FROM ai_sessions WHERE runtime = 'durable'`
  console.log(JSON.stringify({ summary: dryRun ? 'dry-run' : 'recovered', counts, failed, durable_rows: left?.n ?? 0 }))
} finally {
  await connection.close()
  await sql.end()
}
process.exit(failed > 0 ? 1 : 0)
