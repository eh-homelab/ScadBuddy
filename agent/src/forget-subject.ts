import { Client, Connection } from '@temporalio/client'
import { AuditLog } from './audit/log.js'
import { forgetCli } from './durable/forget.js'
import { loadConfig } from './config.js'
import { connectDatabase } from './db.js'
import { loadKek } from './secrets.js'
import { PgPayloadKeys } from './temporal/payloadKeys.js'

// `node dist/forget-subject.js session-<uuid>`: crypto-shreds a durable session
// (durable/forget.ts, spec 2026-10-01 §6.5). Reads the same configuration as main.ts, prints
// the result as JSON and exits 1 on an error. With no SCADBUDDY_TEMPORAL_ADDRESS the
// workflow step is skipped and the result says `workflow: "absent"`.
// The Temporal client carries no payload codec: it only terminates and deletes.

const subject = process.argv[2]
if (!subject || process.argv.length > 3) {
  console.error('usage: forget-subject session-<uuid>')
  process.exit(2)
}

const config = loadConfig()
if (!config.databaseUrl) {
  console.error('SCADBUDDY_DATABASE_URL is not set')
  process.exit(1)
}
const kek = await loadKek(config.secretKeyFile)
if (!kek.ok) {
  console.error(`secret key: ${kek.detail ?? kek.reason}`)
  process.exit(1)
}
const previous = config.previousSecretKeyFile
  ? await loadKek(config.previousSecretKeyFile, 'SCADBUDDY_SECRET_KEY_PREVIOUS_FILE')
  : undefined

const database = connectDatabase(config.databaseUrl, { onMigrationError: (err) => console.error('migration failed:', err) })
let exitCode: 0 | 1 = 1
try {
  if (!(await database.ready())) {
    console.error('the database is not ready')
  } else {
    const keys = new PgPayloadKeys(database.sql, { current: kek.kek, ...(previous?.ok ? { previous: previous.kek } : {}) })
    const client = config.temporalAddress
      ? new Client({ connection: Connection.lazy({ address: config.temporalAddress }), namespace: config.temporalNamespace })
      : undefined
    const audit = new AuditLog({ sql: database.sql, onError: (err) => console.error('audit write failed:', err) })
    const result = await forgetCli(subject, {
      sql: database.sql,
      keys,
      ...(client ? { client } : {}),
      audit,
      actor: { kind: 'operator', id: 'cli', label: 'forget-subject' },
    })
    console.log(JSON.stringify(result.output))
    exitCode = result.exitCode
    await client?.connection.close()
  }
} catch (err) {
  console.error(err instanceof Error ? err.message : String(err))
} finally {
  await database.close()
}
process.exit(exitCode)
