import { Client, Connection } from '@temporalio/client'
import { loadConfig } from './config.js'
import { ExchangeCredential, withServiceCredential } from './api/serviceCredential.js'
import { forgetRoutes, forgetSubject } from './sessions/forget.js'
import path from 'node:path'
import { DEFAULT_STATE_DIR } from './harness/options.js'
import { FilePayloadStore } from './temporal/payloadCodec.js'
import { DURABLE_TURNS_DIR, removeTurnDir } from './temporal/turnServers.js'
import { sessionsRoot } from './harness/stateDirs.js'
import { removeSessionDir } from './workspace/directory.js'

// `node dist/forget-subject.js session-<uuid>`: an operator's forgetSubject
// (spec 2026-10-01 §6.5, sessions/forget.ts), with the agent service's own
// configuration. Every step that touches data goes through the API's CRUD routes, with the
// agent's service credential as main.ts gets it. Prints what it removed; never a payload.

const subject = process.argv[2]
if (!subject) {
  console.error('usage: forget-subject <session-<uuid> | flow-<uuid>>')
  process.exit(2)
}
const config = loadConfig()
const proofFile = config.serviceTokenFile ?? config.serviceBootstrapFile
if (proofFile === undefined) {
  console.error('forget-subject: SCADBUDDY_SERVICE_TOKEN_FILE or SCADBUDDY_SERVICE_BOOTSTRAP_FILE is not set')
  process.exit(2)
}
const connection = config.temporalAddress ? await Connection.connect({ address: config.temporalAddress }) : undefined
if (!connection) console.error('forget-subject: SCADBUDDY_TEMPORAL_ADDRESS is not set; the workflow is left to retention')
try {
  const client = connection ? new Client({ connection, namespace: config.temporalNamespace }) : undefined
  const payloads = config.payloadDir ? new FilePayloadStore(config.payloadDir) : undefined
  if (!payloads) console.error('forget-subject: SCADBUDDY_PAYLOAD_DIR is not set; stored payloads stay (unreadable)')
  // This pod's saved http bodies; another pod's go to its own sweep (sweepTurnDirs).
  const turnDirs = (s: string) => removeTurnDir(path.join(DEFAULT_STATE_DIR, DURABLE_TURNS_DIR), s)
  // The session's working directory, where SCADBUDDY_SESSION_DIR says, on the pod this runs on; each worker's sweep removes its own copy otherwise.
  const sessionsDir = sessionsRoot({ stateDir: DEFAULT_STATE_DIR, sessionsDir: config.sessionDir })
  const sessionDirs = (id: string) => removeSessionDir(sessionsDir, id)
  const credential = new ExchangeCredential({ backendUrl: config.backendUrl, proofFile, proof: config.serviceTokenFile !== undefined ? 'k8s' : 'bootstrap' })
  const crudFetch = withServiceCredential(globalThis.fetch, credential, config.backendUrl)
  const routes = forgetRoutes({ baseUrl: config.backendUrl, fetch: crudFetch })
  const done = await forgetSubject({ client, payloads, turnDirs, sessionDirs, ...routes }, subject)
  console.log(JSON.stringify({ subject, ...done }))
} finally {
  await connection?.close()
}
