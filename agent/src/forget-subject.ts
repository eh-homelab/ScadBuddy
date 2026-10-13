import { Client, Connection } from '@temporalio/client'
import { loadConfig } from './config.js'
import { connectDatabase } from './db.js'
import { forgetSubject } from './sessions/forget.js'
import path from 'node:path'
import { DEFAULT_STATE_DIR } from './harness/options.js'
import { FilePayloadStore } from './temporal/payloadCodec.js'
import { DURABLE_TURNS_DIR, removeTurnDir } from './temporal/turnServers.js'

// `node dist/forget-subject.js session-<uuid>`: an operator's forgetSubject
// (spec 2026-10-01 §6.5, sessions/forget.ts), with the agent service's own
// configuration. Prints what it removed; never a payload.

const subject = process.argv[2]
if (!subject) {
  console.error('usage: forget-subject <session-<uuid> | flow-<uuid>>')
  process.exit(2)
}
const config = loadConfig()
if (!config.databaseUrl) {
  console.error('forget-subject: SCADBUDDY_DATABASE_URL is not set')
  process.exit(2)
}
const database = connectDatabase(config.databaseUrl)
if (!(await database.ready())) {
  console.error('forget-subject: the database is not reachable')
  process.exit(1)
}
const connection = config.temporalAddress ? await Connection.connect({ address: config.temporalAddress }) : undefined
if (!connection) console.error('forget-subject: SCADBUDDY_TEMPORAL_ADDRESS is not set; the workflow is left to retention')
try {
  const client = connection ? new Client({ connection, namespace: config.temporalNamespace }) : undefined
  const payloads = config.payloadDir ? new FilePayloadStore(config.payloadDir) : undefined
  if (!payloads) console.error('forget-subject: SCADBUDDY_PAYLOAD_DIR is not set; stored payloads stay (unreadable)')
  // This pod's saved http bodies; another pod's go to its own sweep (sweepTurnDirs).
  const turnDirs = (s: string) => removeTurnDir(path.join(DEFAULT_STATE_DIR, DURABLE_TURNS_DIR), s)
  const done = await forgetSubject({ sql: database.sql, client, payloads, turnDirs }, subject)
  console.log(JSON.stringify({ subject, ...done }))
} finally {
  await connection?.close()
  await database.sql.end()
}
