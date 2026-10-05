import { Client, Connection, WorkflowNotFoundError } from '@temporalio/client'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { randomBytes, randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { migrate } from '../src/db/migrations.js'
import { DURABLE_TASK_QUEUE, DURABLE_WORKFLOW } from '../src/durable/client.js'
import { ForgetIncompleteError, forgetCli, forgetSubject } from '../src/durable/forget.js'
import { kekFromBase64, SealError } from '../src/secrets.js'
import { SubjectPayloadCodec } from '../src/temporal/codec.js'
import { PgPayloadKeys } from '../src/temporal/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// forgetSubject (spec 2026-10-01 §6.5, plan task 13, #1056): the key first, then the
// workflow, then our rows; a failed workflow step leaves the rows for a re-run.

const WORKFLOWS = fileURLToPath(new URL('./support/durableWorkflow.ts', import.meta.url))
const ACTOR = { kind: 'operator', id: 'cli', label: 'forget-subject' }
const POLL_MS = 120_000

describe.skipIf(!TEST_DATABASE_URL)(
  `forgetSubject${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let keys: PgPayloadKeys
    let id: string
    let subject: string

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      await migrate(db.sql)
      keys = new PgPayloadKeys(db.sql, { current: kekFromBase64(randomBytes(32).toString('base64')) })
      id = randomUUID()
      subject = `session-${id}`
      await keys.createKey(subject)
    })
    afterEach(async () => {
      await drop()
    })

    async function seed(sessionId: string, claudeIds: string[]): Promise<void> {
      await db.sql`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
        VALUES (${sessionId}, 'chat', 'user', 'u', 'U', 'user', 'u', 'idle', 5, 1)`
      await db.sql`INSERT INTO ai_session_events (session_id, seq, event) VALUES (${sessionId}, 1, '{}')`
      await db.sql`INSERT INTO ai_durable_streams (session_id) VALUES (${sessionId})`
      await db.sql`INSERT INTO ai_durable_snapshots (session_id, version, state) VALUES (${sessionId}, 1, '{}')`
      for (const [index, claudeId] of claudeIds.entries()) {
        await db.sql`
          INSERT INTO ai_durable_segments (session_id, segment_index, attempt, claude_session_id)
          VALUES (${sessionId}, 0, ${index}, ${claudeId})`
        await db.sql`INSERT INTO ai_session_entries (project_key, session_id, entry) VALUES ('p', ${claudeId}, '{}')`
        await db.sql`INSERT INTO ai_session_entries (project_key, session_id, subpath, entry) VALUES ('p', ${claudeId}, 'sub', '{}')`
      }
    }

    const count = async (table: string): Promise<number> =>
      Number((await db.sql.unsafe(`SELECT count(*) AS n FROM ${table}`))[0]!['n'])

    it('deletes the key and every row of the session, and nothing of another', async () => {
      const codec = new SubjectPayloadCodec(keys)
      const context = { type: 'workflow', namespace: 'n', workflowId: subject } as const
      const sealed = await codec.encode([{ metadata: {}, data: Buffer.from('secret') }], context)
      const other = randomUUID()
      await seed(id, ['claude-a', 'claude-b'])
      await seed(other, ['claude-other'])

      const result = await forgetSubject(subject, { sql: db.sql, keys })

      // 2 + 2 entries per claude id (4), 2 segments, stream, snapshot, event, session
      expect(result).toEqual({ keyDeleted: true, workflow: 'absent', rows: 4 + 2 + 1 + 1 + 1 + 1 })
      expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${subject}`).toHaveLength(0)
      await expect(codec.decode(sealed)).rejects.toThrow(SealError)
      expect(await db.sql`SELECT 1 FROM ai_sessions WHERE id = ${id}`).toHaveLength(0)
      expect(await db.sql`SELECT 1 FROM ai_session_entries WHERE session_id IN ('claude-a', 'claude-b')`).toHaveLength(0)
      expect(await count('ai_durable_segments')).toBe(1)
      expect(await count('ai_durable_streams')).toBe(1)
      expect(await count('ai_durable_snapshots')).toBe(1)
      expect(await count('ai_session_events')).toBe(1)
      expect(await count('ai_session_entries')).toBe(2)

      expect(await forgetSubject(subject, { sql: db.sql, keys })).toEqual({ keyDeleted: false, workflow: 'absent', rows: 0 })
    })

    it.each([`flow-${randomUUID()}`, 'session-x', `session-${randomUUID()}-x`, 'render-1', `SESSION-${randomUUID()}`])(
      'refuses %s before deleting anything',
      async (bad) => {
        await seed(id, ['claude-a'])
        await expect(forgetSubject(bad, { sql: db.sql, keys })).rejects.toThrow(/not a session-<uuid> subject/)
        expect(await count('ai_payload_keys')).toBe(1)
        expect(await count('ai_sessions')).toBe(1)
        expect(await count('ai_session_entries')).toBe(2)
      },
    )

    it('with no Temporal address, succeeds with workflow absent and audits it', async () => {
      await seed(id, ['claude-a'])
      const audit = new AuditLog({ sql: db.sql })
      const { exitCode, output } = await forgetCli(subject, { sql: db.sql, keys, audit, actor: ACTOR })
      expect(exitCode).toBe(0)
      expect(output).toMatchObject({ subject, workflow: 'absent', keyDeleted: true })
      expect(output['note']).toMatch(/no Temporal address/)
      const [row] = await db.sql`SELECT kind, action, outcome, detail FROM ai_audit`
      expect(row).toMatchObject({ kind: 'operator', action: 'forget_subject', outcome: 'ok' })
      expect(JSON.parse(row!['detail'])).toMatchObject({ complete: true, workflow: 'absent' })
    })

    it('refused subject exits 1 and audits nothing', async () => {
      const audit = new AuditLog({ sql: db.sql })
      const { exitCode } = await forgetCli(`flow-${randomUUID()}`, { sql: db.sql, keys, audit, actor: ACTOR })
      expect(exitCode).toBe(1)
      expect(await count('ai_audit')).toBe(0)
    })

    describe.skipIf(!TEMPORAL_CLI)(`over Temporal${TEMPORAL_SKIP}`, () => {
      let env: TestWorkflowEnvironment
      let worker: Worker
      let running: Promise<void>

      beforeAll(async () => {
        env = await localTemporal()
      }, 60_000)
      afterAll(async () => {
        await env?.teardown()
      })
      beforeEach(async () => {
        worker = await Worker.create({
          connection: env.nativeConnection,
          namespace: 'default',
          taskQueue: DURABLE_TASK_QUEUE,
          workflowsPath: WORKFLOWS,
        })
        running = worker.run()
      }, 90_000)
      afterEach(async () => {
        worker.shutdown()
        await running
      })

      const start = () =>
        env.client.workflow.start(DURABLE_WORKFLOW, {
          workflowId: subject,
          taskQueue: DURABLE_TASK_QUEUE,
          args: [{ session_id: id }, null, null],
        })

      async function gone(): Promise<boolean> {
        const deadline = Date.now() + 60_000
        for (;;) {
          try {
            await env.client.workflow.getHandle(subject).describe()
          } catch (err) {
            return err instanceof WorkflowNotFoundError
          }
          if (Date.now() > deadline) return false
          await new Promise((r) => setTimeout(r, 200))
        }
      }

      it(
        'terminates the open workflow and deletes it, then a second call reports absent',
        async () => {
          await start()
          await seed(id, ['claude-a'])
          const result = await forgetSubject(subject, { sql: db.sql, keys, client: env.client })
          expect(result).toMatchObject({ keyDeleted: true, workflow: 'terminated' })
          expect(await gone()).toBe(true)
          expect(await count('ai_sessions')).toBe(0)
          expect(await forgetSubject(subject, { sql: db.sql, keys, client: env.client })).toEqual({
            keyDeleted: false,
            workflow: 'absent',
            rows: 0,
          })
        },
        POLL_MS,
      )

      it(
        'deletes a workflow that already closed',
        async () => {
          const handle = await start()
          await handle.terminate('test')
          const result = await forgetSubject(subject, { sql: db.sql, keys, client: env.client })
          expect(result).toMatchObject({ workflow: 'closed' })
          expect(await gone()).toBe(true)
        },
        POLL_MS,
      )

      it(
        'an unreachable Temporal fails loudly after the key, keeps the rows, and a re-run completes',
        async () => {
          await start()
          await seed(id, ['claude-a'])
          const audit = new AuditLog({ sql: db.sql })
          const unreachable = new Client({
            connection: Connection.lazy({ address: '127.0.0.1:1', connectTimeout: '1s' }),
            namespace: 'default',
          })
          try {
            await expect(forgetSubject(subject, { sql: db.sql, keys, client: unreachable, rpcDeadlineMs: 3000 })).rejects.toThrow(
              ForgetIncompleteError,
            )
          } finally {
            await unreachable.connection.close()
          }
          expect(await count('ai_payload_keys')).toBe(0)
          expect(await count('ai_sessions')).toBe(1)

          // The CLI path: key already gone, the workflow step fails again.
          const failing = new Client({
            connection: Connection.lazy({ address: '127.0.0.1:1', connectTimeout: '1s' }),
            namespace: 'default',
          })
          let failed
          try {
            failed = await forgetCli(subject, { sql: db.sql, keys, client: failing, rpcDeadlineMs: 3000, audit, actor: ACTOR })
          } finally {
            await failing.connection.close()
          }
          expect(failed.exitCode).toBe(1)
          expect(failed.output).toMatchObject({ complete: false, failed_step: 'workflow' })
          const [row] = await db.sql`SELECT kind, action, outcome, detail FROM ai_audit`
          expect(row).toMatchObject({ kind: 'operator', action: 'forget_subject', outcome: 'error' })
          expect(JSON.parse(row!['detail'])).toMatchObject({ complete: false, failed_step: 'workflow', keyDeleted: false })
          expect(await count('ai_sessions')).toBe(1)

          const rerun = await forgetCli(subject, { sql: db.sql, keys, client: env.client, audit, actor: ACTOR })
          expect(rerun.exitCode).toBe(0)
          expect(rerun.output).toMatchObject({ keyDeleted: false, workflow: 'terminated' })
          expect(await gone()).toBe(true)
          expect(await count('ai_sessions')).toBe(0)
          expect(await count('ai_session_entries')).toBe(0)
        },
        POLL_MS * 2,
      )
    })
  },
)
