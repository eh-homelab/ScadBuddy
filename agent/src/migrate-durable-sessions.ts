import { Client, Connection } from '@temporalio/client'
import { AuditLog } from './audit/log.js'
import { loadConfig } from './config.js'
import { connectDatabase } from './db.js'
import { loadKek } from './secrets.js'
import { EventLog } from './sessions/eventLog.js'
import { backfill, DurableMigration } from './sessions/migrateDurable.js'
import { PostgresSessionStore } from './sessions/store.js'
import { FilePayloadStore, PgPayloadKeys, subjectDataConverter } from './temporal/payloadCodec.js'

// `node dist/migrate-durable-sessions.js [--dry-run] [--stop-on-failure]`: the backfill of plan Step 7.2 (spec
// 2026-10-13 §10.2 step 3, #4772). Every open DurableSession is moved to AgentSession, as its
// next send would move it (sessions/migrateDurable.ts), with a turn that still runs stopped first.
// Run it from one agent pod with the service's configuration: it needs Postgres, Temporal and the
// KEK (the conversation is sealed under each session's data key, in Temporal and in
// ai_session_entries). Prints one JSON line per session and a summary; never a payload.
//
// `--dry-run` changes nothing: a list of the open DurableSessions, and for each a `fork_point`,
// `turn` and `pending_input` Query (no Update, no Signal, no write; migrateDurable.ts `backfill`).
// It reports each one's Claude Code versions, entry count and state (parked, busy, at a deferral
// marker, idle or empty), and every entry type's fields across them. `--stop-on-failure` ends the
// run at the first session that fails, and leaves the rest as they are.

const FLAGS = ['--dry-run', '--stop-on-failure']
const dryRun = process.argv.includes('--dry-run')
const stopOnFailure = process.argv.includes('--stop-on-failure')
const unknown = process.argv.slice(2).filter((a) => !FLAGS.includes(a))
if (unknown.length > 0) {
  console.error('usage: migrate-durable-sessions [--dry-run] [--stop-on-failure]')
  process.exit(2)
}
const config = loadConfig()
if (!config.databaseUrl || !config.temporalAddress) {
  console.error('migrate-durable-sessions: SCADBUDDY_DATABASE_URL and SCADBUDDY_TEMPORAL_ADDRESS must be set')
  process.exit(2)
}
const kek = await loadKek(config.secretKeyFile)
if (!kek.ok) {
  console.error(`migrate-durable-sessions: the secret key is needed (${kek.reason})`)
  process.exit(2)
}
const database = connectDatabase(config.databaseUrl)
if (!(await database.ready())) {
  console.error('migrate-durable-sessions: the database is not reachable')
  process.exit(1)
}
const sql = database.sql
const previous = config.previousSecretKeyFile ? await loadKek(config.previousSecretKeyFile, 'SCADBUDDY_SECRET_KEY_PREVIOUS_FILE') : undefined
const keys = new PgPayloadKeys(sql, kek.kek, { previous: previous?.ok ? previous.kek : undefined })
const connection = await Connection.connect({ address: config.temporalAddress })
let failed: number
try {
  const client = new Client({
    connection,
    namespace: config.temporalNamespace,
    dataConverter: subjectDataConverter(keys, config.payloadDir ? new FilePayloadStore(config.payloadDir) : undefined),
  })
  const migration = new DurableMigration({
    client,
    sql,
    store: new PostgresSessionStore(sql, { keys }),
    events: new EventLog(sql),
    audit: new AuditLog({ sql }),
    // A Query on a DurableSession no worker holds in its cache replays its whole history first:
    // 13 s for one of 1,982 entries on 2026-11-01, so the 10 s default failed 7 of 62 sessions.
    callTimeoutMs: 60_000,
  })
  const report = await backfill(client, sql, migration, { dryRun, stopOnFailure })
  for (const line of report.lines) console.log(JSON.stringify(line))
  failed = report.failed
  console.log(
    JSON.stringify({
      summary: dryRun ? 'dry-run' : 'migrated',
      listed: report.lines.length,
      counts: report.counts,
      failed: report.failed,
      ...(report.stopped ? { stopped_at_first_failure: true } : {}),
      open_durable_sessions: report.open,
      ...(dryRun ? { shapes: report.shapes } : {}),
    }),
  )
} finally {
  await connection.close()
  await sql.end()
}
process.exit(failed > 0 ? 1 : 0)
