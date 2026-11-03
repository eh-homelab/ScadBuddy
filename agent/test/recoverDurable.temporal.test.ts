import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { Client, WorkflowNotFoundError } from '@temporalio/client'
import { Worker } from '@temporalio/worker'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { sessionWorkflowId } from '../src/gate/durable.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { decodeWith, durableRows, recoverDurableSession, type RecoverDeps } from '../src/sessions/recoverDurable.js'
import { SubjectPayloadCodec, subjectDataConverter, type PayloadKeys } from '../src/temporal/payloadCodec.js'
import { SESSION_WORKFLOW } from '../src/temporal/session/names.js'
import type { Step } from './support/closedDurableSession.js'
import { type FakeAnthropic, type RecordedRequest, startFakeAnthropic } from './support/fakeAnthropic.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { AGENT_SESSIONS_SKIP, agentSessions, agentSessionsEnv } from './support/agentSessions.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, stopManagers, tempPaths } from './support/sessions.js'

// #4772: a DurableSession that closed before plan Step 7.2 left its row `runtime = durable` with
// no ai_session_entries. Its conversation is rebuilt from the closed run's history
// (sessions/recoverDurable.ts), and its next send is an AgentSession turn that resumes it. The
// closed run is a stand-in (support/closedDurableSession.ts) whose segments carry Step 7.1's
// recorded conversation (fixtures/durable_conversation/, a real DurableSession's), sealed by the
// payload codec as agent-durable's worker sealed them; the turn after it is the real one
// (support/agentSessions.ts), on the real SDK and its bundled Claude Code against the fake
// Anthropic endpoint.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const STANDIN = fileURLToPath(new URL('./support/closedDurableSession.ts', import.meta.url))
const STANDIN_QUEUE = `closed-durable-${randomUUID().slice(0, 8)}`
const FIXTURE = new URL('./fixtures/durable_conversation/', import.meta.url)
const TOKEN = 'gw-recover-durable-token-12123434565678'
const NEXT = 'And what name is on it?'
const FIRST_PROMPT = 'On the name-keychain model, change the name to Ada'

type Meta = { session_id: string; checkpoint: string; total: number }
type Entry = { type: string; uuid?: string; attachment?: { type?: string; toolUseID?: string } }

async function fixture(): Promise<{ entries: Entry[]; meta: Meta }> {
  const lines = (await readFile(new URL('entries.jsonl', FIXTURE), 'utf8')).split('\n').filter((l) => l.trim())
  return { entries: lines.map((l) => JSON.parse(l) as Entry), meta: JSON.parse(await readFile(new URL('meta.json', FIXTURE), 'utf8')) as Meta }
}

/**
 * The fixture's conversation as the plugin's workflow committed it, segment by segment: each
 * paused at the next tool call (the deferral marker its checkpoint), the call's activity, and
 * the next segment delivering its result; the last one is the second turn's answer.
 */
async function steps(): Promise<Step[]> {
  const { entries, meta } = await fixture()
  const marker = (id: string) => entries.findIndex((e) => e.attachment?.type === 'hook_deferred_tool' && e.attachment.toolUseID === id)
  const cuts = [
    { end: marker('toolu_000001') + 1, call: { id: 'toolu_000001', name: 'get_schema' } },
    { end: marker('toolu_000003') + 1, call: { id: 'toolu_000003', name: 'render_model' } },
    // The first turn's answer and its last-prompt entry.
    { end: entries.findIndex((e, i) => i > marker('toolu_000003') && e.type === 'assistant') + 2, call: undefined },
    { end: entries.length, call: undefined },
  ]
  const out: Step[] = []
  let keep = 0
  let injected: Record<string, unknown> = {}
  for (const [i, cut] of cuts.entries()) {
    const added = entries.slice(keep, cut.end)
    const checkpoint = i === cuts.length - 1 ? meta.checkpoint : [...added].reverse().find((e) => e.uuid)!.uuid!
    out.push({
      segment: {
        input: { session_id: meta.session_id, prompt: i === 0 || i === 3 ? 'prompt' : null, conversation: { query: '__claude_agent_conversation', agent: '', entries: keep }, injected },
        output: {
          session_id: meta.session_id,
          checkpoint,
          transcript_keep: keep,
          transcript_add: added,
          deferred: cut.call ? { id: cut.call.id, name: cut.call.name, input: {}, kind: 'durable' } : null,
          siblings: [],
          result: cut.call ? null : 'done',
          is_error: false,
        },
      },
    })
    injected = {}
    if (cut.call) {
      const result = { done: cut.call.name }
      out.push({ tool: { name: cut.call.name, id: cut.call.id, input: {}, result } })
      injected = { [cut.call.id]: { content: result, is_error: false, blocks: null } }
    }
    keep = cut.end
  }
  return out
}

const said = (r: RecordedRequest | undefined) => JSON.stringify(r?.body?.messages ?? [])

describe.skipIf(skip !== undefined || AGENT_SESSIONS_SKIP !== '')(
  `recovering a closed DurableSession's conversation${skip ? ` (skipped: ${skip})` : ''}${AGENT_SESSIONS_SKIP}`,
  { timeout: 180_000 },
  () => {
    agentSessions()
    let standin: Worker
    let standinRun: Promise<void>
    let fake: FakeAnthropic
    let db: Database
    let drop: () => Promise<void>
    let mine: Set<string>
    // The stand-in worker is the file's; the keys it seals under are each test's database's.
    let keys: PayloadKeys | undefined
    const testKeys: PayloadKeys = { keyFor: (subject, create, options) => keys!.keyFor(subject, create, options) }

    beforeAll(async () => {
      const reply = async (input: { reply: unknown }) => input.reply
      standin = await Worker.create({
        connection: agentSessionsEnv().nativeConnection,
        taskQueue: STANDIN_QUEUE,
        workflowsPath: STANDIN,
        // Sealed as agent-durable's worker sealed a DurableSession's payloads (payloadCodec.ts).
        dataConverter: subjectDataConverter(testKeys),
        activities: { run_claude_segment: reply, get_schema: reply, render_model: reply },
      })
      standinRun = standin.run()
    }, 120_000)
    afterAll(async () => {
      standin?.shutdown()
      await standinRun
    })
    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      keys = testPayloadKeys(db.sql)
      fake = await startFakeAnthropic(() => ({ text: 'The name is Ada.' }))
      mine = new Set()
    })
    afterEach(async () => {
      await stopManagers()
      for (const id of mine) {
        await agentSessionsEnv()
          .client.workflow.getHandle(sessionWorkflowId(id))
          .terminate('the test ended')
          .catch((err: unknown) => {
            if (!(err instanceof WorkflowNotFoundError)) throw err
          })
      }
      await fake.close()
      await drop()
    })

    async function agent(): Promise<SessionManager> {
      const paths = await tempPaths()
      await ensureStateDirs(paths)
      return manager({
        sql: db.sql,
        paths,
        payloadKeys: keys,
        credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
        settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      })
    }

    const deps = (m: SessionManager): RecoverDeps => ({
      client: agentSessionsEnv().client,
      sql: db.sql,
      decode: decodeWith(new SubjectPayloadCodec(keys!)),
      store: m.store,
    })

    /** A session whose DurableSession ran Step 7.1's two turns and completed before Step 7.2: its row left `durable`. */
    async function closedDurableSession(m: SessionManager): Promise<string> {
      const { session } = await m.start(browser, { origin: 'chat', title: 'keychain' })
      mine.add(session.id)
      await db.sql`UPDATE ai_sessions SET runtime = 'durable' WHERE id = ${session.id}`
      // A client that opens the stand-in's sealed payloads, as the service's own does.
      const client = new Client({ connection: agentSessionsEnv().client.connection, namespace: agentSessionsEnv().client.options.namespace, dataConverter: subjectDataConverter(keys!) })
      const handle = await client.workflow.start('DurableSession', {
        workflowId: sessionWorkflowId(session.id),
        taskQueue: STANDIN_QUEUE,
        args: [await steps()],
      })
      await handle.result()
      return session.id
    }

    /** Retention: the closed run leaves Temporal's persistence (in production Archival keeps it), so a new run may take the id. */
    async function retire(id: string): Promise<void> {
      const client = agentSessionsEnv().client
      const { runId } = await client.workflow.getHandle(sessionWorkflowId(id)).describe()
      await client.workflowService.deleteWorkflowExecution({ namespace: client.options.namespace, workflowExecution: { workflowId: sessionWorkflowId(id), runId } })
      // The dev server deletes a closed run in about 20 s.
      const until = Date.now() + 90_000
      for (;;) {
        const gone = await client.workflow
          .getHandle(sessionWorkflowId(id))
          .describe()
          .then(
            () => false,
            (err: unknown) => err instanceof WorkflowNotFoundError,
          )
        if (gone) return
        if (Date.now() > until) throw new Error('the closed run was not deleted')
        await new Promise((resolve) => setTimeout(resolve, 200))
      }
    }

    it("a recovered session's next turn resumes its old turns", async () => {
      const m = await agent()
      const id = await closedDurableSession(m)
      const { meta } = await fixture()

      const dry = await recoverDurableSession(deps(m), id, { dryRun: true })
      expect(dry).toMatchObject({ outcome: 'recovered', segments: 4, committed: 4, conversation: meta.total, entries: meta.total, checkpoint: 'assistant', versions: ['2.1.292'] })
      expect(dry.run).toMatchObject({ status: 'COMPLETED', archived: false })
      // The rebuild's held results, derived from the tool activities, agree with what each next segment delivered.
      expect(dry).toMatchObject({ derived_checked: 2, derived_matched: 2 })
      expect(await db.sql`SELECT 1 FROM ai_session_entries WHERE session_id = ${id}`).toHaveLength(0)
      expect(await durableRows(db.sql)).toEqual([id])

      const done = await recoverDurableSession(deps(m), id)
      expect(done).toMatchObject({ outcome: 'recovered', entries: meta.total })
      const [row] = await db.sql<{ runtime: string; status: string }[]>`SELECT runtime, status FROM ai_sessions WHERE id = ${id}`
      expect(row).toEqual({ runtime: 'unified', status: 'idle' })
      // Sealed under the session's id, every entry naming it.
      const stored = (await m.store.load({ projectKey: '', sessionId: id }))!
      expect(stored).toHaveLength(meta.total)
      expect(new Set(stored.map((e) => (e as { sessionId?: string }).sessionId).filter(Boolean))).toEqual(new Set([id]))
      // Once recovered the row is no closed DurableSession's: a second run does nothing.
      expect(await recoverDurableSession(deps(m), id)).toEqual({ session: id, outcome: 'not_durable' })
      expect(await db.sql`SELECT 1 FROM ai_session_entries WHERE session_id = ${id}`).toHaveLength(meta.total)

      await retire(id)
      const turn = await m.send(id, browser, NEXT)
      expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
      expect((await agentSessionsEnv().client.workflow.getHandle(sessionWorkflowId(id)).describe()).type).toBe(SESSION_WORKFLOW)
      // The turn's model request carries the old turns, their tool calls and results, then the prompt.
      const request = said(fake.messageCalls().at(-1))
      for (const part of [FIRST_PROMPT, 'toolu_000001', 'toolu_000003', 'Thanks. Which colour did you use for the letters?', 'I used red (#ff0000)', NEXT]) {
        expect(request).toContain(part)
      }
      expect(request.indexOf(FIRST_PROMPT)).toBeLessThan(request.indexOf(NEXT))
    })

    it('a session with no closed run, or a row that is not durable, is left alone', async () => {
      const m = await agent()
      const { session } = await m.start(browser, { origin: 'chat', title: 'never ran' })
      expect(await recoverDurableSession(deps(m), session.id)).toEqual({ session: session.id, outcome: 'not_durable' })
      await db.sql`UPDATE ai_sessions SET runtime = 'durable' WHERE id = ${session.id}`
      expect(await recoverDurableSession(deps(m), session.id)).toMatchObject({ session: session.id, outcome: 'no_history' })
      const [row] = await db.sql<{ runtime: string }[]>`SELECT runtime FROM ai_sessions WHERE id = ${session.id}`
      expect(row?.runtime).toBe('durable')
    })
  },
)
