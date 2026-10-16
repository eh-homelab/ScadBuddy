import { Client } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { ApplicationFailure } from '@temporalio/common'
import { randomBytes, randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { kekFromBase64 } from '../src/secrets.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { PgPayloadKeys, rewrapPayloadKeys, SubjectForgotten, SubjectPayloadCodec } from '../src/temporal/payloadCodec.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// A durable session's payloads on a real Temporal (spec 2026-10-01 §6.5): sealed in
// history under the session's own key, and undecryptable once forgetSubject deleted it.

const SKIP = !TEMPORAL_CLI || !TEST_DATABASE_URL
const WHY = TEMPORAL_SKIP || (TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`)
const kek = kekFromBase64(randomBytes(32).toString('base64'))
const WORDS = 'print the red bracket, the private one'

describe.skipIf(SKIP)(`durable payloads and forgetSubject${WHY}`, () => {
  let env: TestWorkflowEnvironment
  let db: Database
  let drop: () => Promise<void>
  beforeAll(async () => {
    env = await localTemporal()
  }, 60_000)
  afterAll(async () => {
    await env?.teardown()
  })
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it("seals a session's history under its key, and forgetSubject shreds it with its rows", async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    await db.sql`UPDATE ai_sessions SET mode = 'durable' WHERE id = ${session.id}`
    const subject = `session-${session.id}`
    const keys = new PgPayloadKeys(db.sql, kek)
    const dataConverter = { payloadCodecs: [new SubjectPayloadCodec(keys)] }
    const client = new Client({ connection: env.connection, namespace: 'default', dataConverter })
    const taskQueue = `codec-${session.id}`
    const worker = await Worker.create({
      connection: env.nativeConnection,
      taskQueue,
      workflowsPath: fileURLToPath(new URL('./support/codecWorkflows.ts', import.meta.url)),
      activities: {
        failWith: async (text: string) => {
          throw ApplicationFailure.nonRetryable(`the tool failed on: ${text}`, 'ToolError')
        },
      },
      // As main.ts gives the agent-tools worker: failures' messages are sealed too.
      dataConverter: { ...dataConverter, failureConverterPath: fileURLToPath(new URL('../src/temporal/failureConverter.ts', import.meta.url)) },
    })
    await worker.runUntil(async () => {
      const handle = await client.workflow.start('holdText', { workflowId: subject, taskQueue, args: [WORDS] })
      await handle.signal('finish')
      expect(await handle.result()).toBe(`${WORDS} (done)`)
      // Not a word of it in the history the server holds.
      const { history } = await env.client.workflowService.getWorkflowExecutionHistory({
        namespace: 'default',
        execution: { workflowId: subject },
      })
      const raw = JSON.stringify(history)
      expect(raw).toContain(Buffer.from('binary/scadbuddy-subject').toString('base64'))
      const bytes = Buffer.from(JSON.stringify(history?.events?.map((e) => e.workflowExecutionStartedEventAttributes?.input)))
      expect(bytes.toString()).not.toContain('red bracket')
      expect(raw).not.toContain(Buffer.from(WORDS).toString('base64').slice(0, 12))

      // A tool's error text is the session's content too: sealed, not a plaintext failure message.
      const flow = `flow-${randomUUID()}`
      expect(await client.workflow.execute('failingTool', { workflowId: flow, taskQueue, args: [WORDS] })).toBe(
        `caught: the tool failed on: ${WORDS}`,
      )
      const { history: failed } = await env.client.workflowService.getWorkflowExecutionHistory({
        namespace: 'default',
        execution: { workflowId: flow },
      })
      expect(JSON.stringify(failed)).not.toContain('red bracket')
      expect(JSON.stringify(failed)).toContain('Encoded failure')
    })

    // A render's workflow is not a subject: its payloads stay as they are.
    const plain = await client.workflow.start('holdText', { workflowId: `render-${session.id}`, taskQueue, args: ['plain'] })
    const { history: renderHistory } = await env.client.workflowService.getWorkflowExecutionHistory({
      namespace: 'default',
      execution: { workflowId: plain.workflowId },
    })
    expect(JSON.stringify(renderHistory)).toContain(Buffer.from('"plain"').toString('base64'))
    await plain.terminate()

    expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${subject}`).toHaveLength(1)
    const removed: string[] = []
    const payloads = { forget: async (s: string) => void removed.push(s) }
    const forgotten = await forgetSubject({ sql: db.sql, client, keys, payloads }, subject)
    expect(forgotten).toEqual({ key: true, workflow: 'deleted', rows: 1 })
    expect(removed).toEqual([subject]) // its stored payloads' directory (#2243)
    expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${subject}`).toHaveLength(0)
    expect(await db.sql`SELECT 1 FROM ai_sessions WHERE id = ${session.id}`).toHaveLength(0)
    expect(await db.sql`SELECT 1 FROM ai_session_events WHERE session_id = ${session.id}`).toHaveLength(0)
    await expect(keys.keyFor(subject, false)).rejects.toBeInstanceOf(SubjectForgotten)
    // An encoder still running for it (before the termination) cannot make it a new key.
    await expect(new PgPayloadKeys(db.sql, kek).keyFor(subject, true)).rejects.toBeInstanceOf(SubjectForgotten)
    await expect(
      new SubjectPayloadCodec(new PgPayloadKeys(db.sql, kek)).encode([{ metadata: {}, data: new Uint8Array([1]) }], {
        type: 'activity',
        namespace: 'default',
        workflowId: subject,
        isLocal: false,
      }),
    ).rejects.toBeInstanceOf(SubjectForgotten)
    expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${subject}`).toHaveLength(0)
    // Again: nothing left, and nothing fails (the server deletes a history in the background).
    expect(await forgetSubject({ sql: db.sql, client, keys }, subject)).toEqual({ key: false, workflow: expect.stringMatching(/^(deleted|not_found)$/), rows: 0 })
    await expect(forgetSubject({ sql: db.sql }, 'render-x')).rejects.toThrow(/not a session/)
  }, 120_000)

  it("re-wraps payload keys from the previous key, and removes a pending entry's rows with the session", async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    const { session } = await m.start(browser, { origin: 'chat', title: 't' })
    const subject = `session-${session.id}`
    const key = await new PgPayloadKeys(db.sql, kek).keyFor(subject, true)
    const next = kekFromBase64(randomBytes(32).toString('base64'))
    expect(await rewrapPayloadKeys(db.sql, kek, next)).toEqual({ rewrapped: 1, failed: 0 })
    expect(await new PgPayloadKeys(db.sql, next).keyFor(subject, false)).toEqual(key)
    await expect(new PgPayloadKeys(db.sql, kek).keyFor(subject, false)).rejects.toThrow(/not mounted/)
    const request = `durable:${session.id}:run-1:toolu_1`
    await db.sql`
      INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind, tool, responders, expires_at)
      VALUES (${request}, ${session.id}, ${subject}, 'run-1', 'answer', 'ask_user', ${db.sql.array(['browser'])}, now() + interval '1 minute')`
    await db.sql`
      INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder)
      VALUES (${`${request}x`}, ${session.id}, 'answer', 'answered', ${db.sql.json(browser)})`
    expect((await forgetSubject({ sql: db.sql }, subject)).workflow).toBe('not_reached')
    expect(await db.sql`SELECT 1 FROM ai_pending_input WHERE session_id = ${session.id}`).toHaveLength(0)
    expect(await db.sql`SELECT 1 FROM ai_input_responses WHERE session_id = ${session.id}`).toHaveLength(0)
  }, 60_000)
})
