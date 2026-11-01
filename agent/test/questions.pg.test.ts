import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { ASK_USER_QUESTION, ASK_USER_TOOL, type QuestionVerdict, type UserQuestion } from '../src/harness/questions.js'
import type { HarnessRun } from '../src/harness/run.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { ATTENTION_RATE_LOCK, QuestionService } from '../src/questions/service.js'
import { respond, RespondError } from '../src/routes/pendingInput.js'
import { ChatConnection } from '../src/routes/chat.js'
import { type Owner, PROTOCOL_VERSION, type ServerEvent } from '../src/sessions/protocol.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, tempPaths } from './support/sessions.js'
import { AGENT_SESSIONS_SKIP, agentSessions } from './support/agentSessions.js'

// A question a turn asks, without the SDK (#940): every session is an AgentSession (plan Step
// 6.3), so AskUserQuestion parks as the gate's `answer` entry (ai_pending_input, its answer in
// ai_input_responses, sessions/turnHost.ts), the session waits in `waiting_input`, only the
// user answers, through the one respond route (routes/pendingInput.ts), and the question never
// outlives its turn. The SDK side is test/questions.e2e.test.ts.

const QUESTIONS: UserQuestion[] = [
  {
    question: 'Which colour should the base be?',
    header: 'Colour',
    multiSelect: false,
    options: [
      { label: 'Red', description: 'PLA red' },
      { label: 'Blue', description: 'PLA blue' },
    ],
  },
  {
    question: 'Approve this issue draft?',
    header: 'Draft',
    multiSelect: false,
    options: [
      { label: 'Approve', description: 'File it as written', preview: '## Title\n\nBody with sk-ant-test inside.' },
      { label: 'Cancel', description: 'Do not file it' },
    ],
  },
]

/** The respond body for one answer per question, keyed by each question's words. */
const answerBody = (answers: string[], questions: UserQuestion[] = QUESTIONS) =>
  ({ kind: 'answer', answers: Object.fromEntries(questions.map((q, i) => [q.question, answers[i]!])) }) as const

/** A question frame as an older panel sends it on the chat socket. */
const socketAnswer = (sessionId: string, id: string, answers: unknown) =>
  ({ v: PROTOCOL_VERSION, type: 'question.answer', sessionId, id, answers }) as const

const result = (run: HarnessRun) =>
  ({ type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume }) as unknown as SDKMessage

describe.skipIf(!TEST_DATABASE_URL || AGENT_SESSIONS_SKIP !== '')(
  `questions in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}${AGENT_SESSIONS_SKIP}`,
  () => {
    agentSessions()
    let db: Database
    let drop: () => Promise<void>
    let verdicts: QuestionVerdict[]
    let runs: HarnessRun[]

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      verdicts = []
      runs = []
    })
    afterEach(async () => {
      await drop()
    })

    /** A turn that asks QUESTIONS through its gate (when it has one) with `tool`, then ends. */
    // Each of a session's calls its own tool_use id (toolu_q1, toolu_q2, …), as the model's are: a
    // second turn that asked as toolu_q1 again would, on the same workflow run, find the first
    // one's answer (#5053's CI).
    const askingWith = (tool: string) => {
      const asked = new Map<string, number>()
      return (run: HarnessRun): AsyncIterable<SDKMessage> => {
        runs.push(run)
        return (async function* () {
          await Promise.resolve()
          if (run.questionGate) {
            const session = run.sessionId ?? run.resume ?? ''
            const n = (asked.get(session) ?? 0) + 1
            asked.set(session, n)
            verdicts.push(await run.questionGate({ tool, questions: QUESTIONS, toolUseId: `toolu_q${n}`, signal: new AbortController().signal }))
          }
          yield result(run)
        })()
      }
    }
    const asking = askingWith(ASK_USER_QUESTION)

    async function events(m: SessionManager, sessionId: string): Promise<ServerEvent[]> {
      return (await m.events.read(sessionId)).map((e) => e.event)
    }
    const statusesOf = async (m: SessionManager, sessionId: string) =>
      (await events(m, sessionId)).flatMap((e) => (e.type === 'session.status' ? [e.status] : []))

    /** The request ids of the session's parked entries of `kind`, oldest first. */
    const parked = async (sessionId: string, kind: 'answer' | 'approval') =>
      (
        await db.sql<{ request_id: string }[]>`
          SELECT request_id FROM ai_pending_input WHERE session_id = ${sessionId} AND kind = ${kind} ORDER BY created_at, request_id`
      ).map((r) => r.request_id)

    /** The one question the session's turn parked on, once the session says it waits for it. */
    async function pendingQuestion(m: SessionManager, sessionId: string): Promise<string> {
      let id: string | undefined
      await expect.poll(async () => (id = (await parked(sessionId, 'answer'))[0]), { timeout: 20_000 }).toBeDefined()
      await expect.poll(async () => (await m.get(sessionId, browser)).status, { timeout: 10_000 }).toBe('waiting_input')
      return id!
    }

    const responseOf = async (requestId: string) =>
      (
        await db.sql<{ outcome: string; response: unknown; reason: string | null; responder_kind: string }[]>`
          SELECT outcome, response, reason, responder->>'kind' AS responder_kind FROM ai_input_responses WHERE request_id = ${requestId}`
      )[0]

    /** How a respond the route refused answered: its status, or 'answered'. */
    const refused = (p: Promise<unknown>) =>
      p.then(
        () => 'answered',
        (err: unknown) => (err instanceof RespondError ? err.status : String(err)),
      )

    it('parks the turn in waiting_input until the user answers, and hands the answers back keyed by question', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(m, session.id)

      expect(await respond(m, browser, id, answerBody(['Blue', 'Approve']))).toMatchObject({ outcome: 'answered' })
      await turn!.done
      expect(verdicts).toEqual([
        { answered: true, answers: { 'Which colour should the base be?': 'Blue', 'Approve this issue draft?': 'Approve' } },
      ])
      expect((await m.get(session.id, browser)).status).toBe('idle')

      const log = await events(m, session.id)
      const asked = log.find((e) => e.type === 'question.asked')
      expect(asked).toMatchObject({ id, tool: 'toolu_q1' })
      // The turn's credential never reaches the log, not even inside a draft.
      expect(JSON.stringify(asked)).not.toContain('sk-ant-test')
      expect(log.find((e) => e.type === 'question.resolved')).toEqual({
        v: PROTOCOL_VERSION,
        type: 'question.resolved',
        sessionId: session.id,
        id,
        answered: true,
        answers: ['Blue', 'Approve'],
        by: browser,
      })
      expect(await statusesOf(m, session.id)).toEqual(['idle', 'running', 'waiting_input', 'running', 'idle'])
      await expectPanelAccepts(log)

      expect(await responseOf(id)).toMatchObject({ outcome: 'answered', response: { answers: ['Blue', 'Approve'] }, responder_kind: 'browser' })
      expect(await parked(session.id, 'answer')).toEqual([])
    })

    // #3255, through the gate: the answer and the session's move back to `running` are one
    // append, in the resolve's own transaction (gate/store.ts `resolved`), so `running` is
    // logged right behind the answer, before the turn can finish and set `idle`.
    it('logs the move back to running with the answer (#3255)', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(m, session.id)
      await respond(m, browser, id, answerBody(['Blue', 'Approve']))
      await turn!.done

      const log = await m.events.read(session.id)
      const at = log.findIndex((e) => e.event.type === 'question.resolved')
      expect(log.slice(at, at + 3).map((e) => e.event.type)).toEqual(['question.resolved', 'input.resolved', 'session.status'])
      expect(log[at + 2]?.event).toMatchObject({ type: 'session.status', status: 'running' })
      expect(log.slice(at, at + 3).map((e) => Number(e.seq) - Number(log[at]!.seq))).toEqual([0, 1, 2])
      expect(await statusesOf(m, session.id)).toEqual(['idle', 'running', 'waiting_input', 'running', 'idle'])
    })

    // Review of #3685, for questions: two answers in one session, each with the other's
    // question still pending when it lands, must leave the session out of `waiting_input`.
    it('two answers to two questions in one session, given together, move it back from waiting_input', async () => {
      const twice = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          await Promise.resolve()
          const ask = (toolUseId: string) => run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId, signal: new AbortController().signal })
          verdicts.push(...(await Promise.all([ask('toolu_q1'), ask('toolu_q2')])))
          yield result(run)
        })()
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: twice })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me twice' })
      let ids: string[] = []
      await expect.poll(async () => (ids = await parked(session.id, 'answer')).length, { timeout: 20_000 }).toBe(2)
      await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_input')

      await Promise.all(ids.map((id) => respond(m, browser, id, answerBody(['Blue', 'Approve']))))
      await turn!.done
      expect(verdicts).toHaveLength(2)
      expect(verdicts.every((v) => v.answered)).toBe(true)
      const statuses = await statusesOf(m, session.id)
      expect(statuses.at(-2)).toBe('running')
      expect(statuses.at(-1)).toBe('idle')
    })

    // #1075: an answer is in the AI audit log, tied to the tool call, as a hash: an answer can
    // be free text the user typed. A question nobody answered is there too, as ScadBuddy's
    // cancellation.
    it.each([ASK_USER_QUESTION, ASK_USER_TOOL])('records the answer to %s, and a cancellation, in the audit log: who, which call, a hash, never the text', async (tool) => {
      const audit = new AuditLog({ sql: db.sql })
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: askingWith(tool), audit })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(m, session.id)
      await respond(m, browser, id, answerBody(['Make it teal, like my car', 'Approve']), { clientIp: '192.0.2.7' })
      await turn!.done
      const rows = await db.sql`
        SELECT action, surface, client_ip, principal_kind, principal_id, session_id, tool_use_id, tier, outcome
        FROM ai_audit WHERE kind = 'question'`
      expect(rows).toEqual([
        {
          action: 'answered',
          surface: 'http',
          client_ip: '192.0.2.7',
          principal_kind: 'browser',
          principal_id: browser.id,
          session_id: session.id,
          tool_use_id: 'toolu_q1',
          tier: 'read',
          outcome: 'ok',
        },
      ])
      expect(JSON.stringify(await db.sql`SELECT * FROM ai_audit`)).not.toContain('teal')

      const next = await m.send(session.id, browser, 'ask again')
      const again = await pendingQuestion(m, session.id)
      expect(await m.interrupt(session.id, browser)).toBe(true)
      await next.done
      expect(await db.sql`SELECT action, surface, principal_kind, outcome FROM ai_audit WHERE kind = 'question' AND action = 'cancelled'`).toEqual([
        { action: 'cancelled', surface: 'system', principal_kind: 'system', outcome: 'refused' },
      ])
      expect(again).not.toBe(id)
    })

    // #1108: a subagent's call is in the feed and has its tool_call row, tagged with the Agent
    // call that spawned it.
    it("a subagent's ask_user has a tool_call row and a tool.call tagged with the Agent call that spawned it", async () => {
      const audit = new AuditLog({ sql: db.sql })
      const say = (parent: string | null, content: unknown[]) =>
        ({ type: 'assistant', message: { id: `msg_${parent ?? 'top'}`, content }, parent_tool_use_id: parent }) as unknown as SDKMessage
      const toolResult = (parent: string | null, id: string, text: string) =>
        ({ type: 'user', message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: id, content: text }] }, parent_tool_use_id: parent }) as unknown as SDKMessage
      const subagent = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          yield say(null, [{ type: 'tool_use', id: 'toolu_agent', name: 'Agent', input: { description: 'ask', prompt: 'ask the user' } }])
          yield say('toolu_agent', [{ type: 'tool_use', id: 'toolu_q1', name: ASK_USER_TOOL, input: { questions: QUESTIONS } }])
          verdicts.push(await run.questionGate!({ tool: ASK_USER_TOOL, questions: QUESTIONS, toolUseId: 'toolu_q1', signal: new AbortController().signal }))
          yield toolResult('toolu_agent', 'toolu_q1', 'answered')
          yield toolResult(null, 'toolu_agent', 'the user picked blue')
          yield result(run)
        })()
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: subagent, audit })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(m, session.id)
      await respond(m, browser, id, answerBody(['Blue', 'Approve']))
      await turn!.done
      expect(verdicts[0]).toMatchObject({ answered: true })

      const log = await events(m, session.id)
      expect(log.filter((e) => e.type === 'tool.call').map((e) => (e.type === 'tool.call' ? [e.id, e.name, e.parent] : []))).toEqual([
        ['toolu_agent', 'Agent', undefined],
        ['toolu_q1', ASK_USER_TOOL, 'toolu_agent'],
      ])
      expect(log.filter((e) => e.type === 'tool.result').map((e) => (e.type === 'tool.result' ? [e.id, e.ok] : []))).toEqual([
        ['toolu_q1', true],
        ['toolu_agent', true],
      ])
      await expectPanelAccepts(log)

      const rows = await db.sql`SELECT kind, action, tool_use_id, tier, outcome FROM ai_audit ORDER BY id`
      expect(rows).toEqual(
        expect.arrayContaining([
          { kind: 'tool_call', action: ASK_USER_TOOL, tool_use_id: 'toolu_q1', tier: 'read', outcome: 'ok' },
          { kind: 'tool_call', action: 'Agent', tool_use_id: 'toolu_agent', tier: expect.any(String), outcome: 'ok' },
        ]),
      )
    })

    it('only the user in the panel answers, once, with one answer per question', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(m, session.id)
      const unknownRun = id.replace(/:[^:]+:([^:]+)$/, ':no-such-run:$1')
      const unknownSession = id.replace(session.id, '00000000-0000-4000-8000-000000000000')

      // An agent is not the user: it may not answer.
      expect(await refused(respond(m, agentA as Owner, id, answerBody(['Red', 'Cancel'])))).toBe(403)
      // One answer for two questions, or a blank one, is not an answer.
      expect(await refused(respond(m, browser, id, { kind: 'answer', answers: { 'Which colour should the base be?': 'Red' } }))).toBe(400)
      expect(await refused(respond(m, browser, id, answerBody(['Red', '  '])))).toBe(400)
      // An approval's body is not an answer.
      expect(await refused(respond(m, browser, id, { kind: 'approval', decision: 'approve' }))).toBe(400)
      expect(await refused(respond(m, browser, unknownRun, answerBody(['Red', 'Cancel'])))).toBe(404)
      expect(await refused(respond(m, browser, unknownSession, answerBody(['Red', 'Cancel'])))).toBe(404)
      // None of those took it.
      expect(await parked(session.id, 'answer')).toEqual([id])

      await respond(m, browser, id, answerBody(['Red', 'Cancel']))
      // Once: a second answer is refused, resolved (409) or, once the run has gone on, stale (404),
      // and the first stands.
      expect([404, 409]).toContain(await refused(respond(m, browser, id, answerBody(['Blue', 'Approve']))))
      expect(await responseOf(id)).toMatchObject({ outcome: 'answered', response: { answers: ['Red', 'Cancel'] } })
      await turn!.done
      expect(verdicts[0]).toMatchObject({ answered: true, answers: { 'Which colour should the base be?': 'Red' } })
    })

    it('a session another principal owns is not given the tool: nobody there would be asked', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking })
      const { turn } = await m.start(agentA, { origin: 'mcp', prompt: 'ask me' })
      await turn!.done
      expect(runs[0]?.questionGate).toBeUndefined()
      expect(verdicts).toEqual([])
    })

    it('an interrupt cancels the question: the model is told nobody answered, and nothing was chosen', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(m, session.id)

      expect(await m.interrupt(session.id, browser)).toBe(true)
      await turn!.done
      expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/did not answer/) }])
      expect(await responseOf(id)).toMatchObject({ outcome: 'cancelled', response: null })
      const resolved = (await events(m, session.id)).find((e) => e.type === 'question.resolved')
      expect(resolved).toMatchObject({ id, answered: false })
      expect(resolved).not.toHaveProperty('answers')
      expect((await m.get(session.id, browser)).status).toBe('idle')
      expect(await parked(session.id, 'answer')).toEqual([])
    })

    /** A turn that parks on a question and an outward call, in `order`, and then waits for both. */
    const askAndPrint = (order: 'question first' | 'approval first') => (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const signal = new AbortController().signal
        const ask = () => run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q', signal })
        const print = () => run.approvalGate!({ toolName: 'mcp__stub__print', input: { job: 'box' }, toolUseId: 'toolu_p', tier: 'outward', signal })
        if (order === 'question first') {
          const asked = ask()
          await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_pending_input WHERE kind = 'answer'`).length, { timeout: 20_000 }).toBe(1)
          const approved = print()
          verdicts.push(await asked)
          await approved
        } else {
          const approved = print()
          await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_pending_input WHERE kind = 'approval'`).length, { timeout: 20_000 }).toBe(1)
          verdicts.push(await ask())
          await approved
        }
        yield result(run)
      })()

    it('a question and an approval pending together: deciding the approval leaves the session waiting for the answer', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: askAndPrint('question first') })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask and print' })
      let approvalId: string | undefined
      await expect.poll(async () => (approvalId = (await parked(session.id, 'approval'))[0]), { timeout: 20_000 }).toBeDefined()
      await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_approval')

      await respond(m, browser, approvalId!, { kind: 'approval', decision: 'approve' })
      await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_input')
      const [q] = await parked(session.id, 'answer')
      await respond(m, browser, q!, answerBody(['Red', 'Approve']))
      await turn!.done
      expect(verdicts[0]).toMatchObject({ answered: true })
      const statuses = await statusesOf(m, session.id)
      // The approval's resolution hands back to the question, never straight to running.
      const resolved = statuses.lastIndexOf('waiting_approval')
      expect(statuses[resolved + 1]).toBe('waiting_input')
      expect(statuses.at(-1)).toBe('idle')
    })

    it('a question and an approval pending together: answering the question leaves the session waiting for the approval', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: askAndPrint('question first') })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask and print' })
      let approvalId: string | undefined
      await expect.poll(async () => (approvalId = (await parked(session.id, 'approval'))[0]), { timeout: 20_000 }).toBeDefined()
      await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_approval')

      const [q] = await parked(session.id, 'answer')
      await respond(m, browser, q!, answerBody(['Red', 'Approve']))
      await expect.poll(() => verdicts.length).toBe(1)
      // The approval is still pending, so the session still says so.
      expect((await m.get(session.id, browser)).status).toBe('waiting_approval')
      await respond(m, browser, approvalId!, { kind: 'approval', decision: 'approve' })
      await turn!.done
      expect((await m.get(session.id, browser)).status).toBe('idle')
    })

    it('an approval asked first, then a question: the session shows the question, then hands back to the approval', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: askAndPrint('approval first') })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'print and ask' })
      const id = await pendingQuestion(m, session.id)

      await respond(m, browser, id, answerBody(['Blue', 'Approve']))
      await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_approval')
      const [approvalId] = await parked(session.id, 'approval')
      await respond(m, browser, approvalId!, { kind: 'approval', decision: 'approve' })
      await turn!.done
      expect(verdicts[0]).toMatchObject({ answered: true })
      const statuses = await statusesOf(m, session.id)
      expect(statuses.indexOf('waiting_input')).toBeGreaterThan(statuses.indexOf('waiting_approval'))
      expect(statuses.at(-1)).toBe('idle')
    })

    it('a handoff cancels the pending question, and the running turn cannot park on another for the new owner', async () => {
      let second: QuestionVerdict | undefined
      const twice = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          await Promise.resolve()
          const signal = new AbortController().signal
          verdicts.push(await run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q1', signal }))
          second = await run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q2', signal })
          yield result(run)
        })()
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: twice })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(m, session.id)

      await m.handoff(session.id, browser, agentA)
      await m.acceptHandoff(session.id, agentA)
      await turn!.done
      expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/did not answer/) }])
      expect(second).toMatchObject({ answered: false })
      // Nothing was recorded for the second call: no card, no wait.
      expect(await db.sql`SELECT request_id, outcome FROM ai_input_responses WHERE session_id = ${session.id}`).toEqual([
        { request_id: id, outcome: 'cancelled' },
      ])
      expect(await parked(session.id, 'answer')).toEqual([])
      await expect.poll(async () => (await m.get(session.id, agentA)).status).toBe('idle')
    })

    it('a call the SDK withdraws while the turn goes on is cancelled, and the session goes back to running', async () => {
      const withdraw = new AbortController()
      let carryOn!: () => void
      const goOn = new Promise<void>((r) => {
        carryOn = r
      })
      const dropped = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          await Promise.resolve()
          verdicts.push(await run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q', signal: withdraw.signal }))
          await goOn
          yield result(run)
        })()
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: dropped })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(m, session.id)

      withdraw.abort()
      await expect.poll(() => verdicts.length).toBe(1)
      expect(verdicts[0]).toMatchObject({ answered: false })
      await expect.poll(async () => (await responseOf(id))?.outcome, { timeout: 10_000 }).toBe('cancelled')
      // The turn is still live, and no longer says it waits for the user.
      await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('running')
      expect(await m.get(session.id, browser)).toMatchObject({ status: 'running', turnActive: true })
      expect((await events(m, session.id)).find((e) => e.type === 'question.resolved')).toMatchObject({ id, answered: false })
      carryOn()
      await turn!.done
    })

    it('a question that redaction makes invalid is refused, not parked where the panel cannot show it', async () => {
      // A label holding the turn's secret collides, once redacted, with one that reads like it.
      const colliding: UserQuestion[] = [
        {
          question: 'Which key?',
          header: 'Key',
          multiSelect: false,
          options: [
            { label: 'Use sk-ant-test', description: '' },
            { label: 'Use [redacted]', description: '' },
          ],
        },
      ]
      const askingColliding = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          await Promise.resolve()
          verdicts.push(await run.questionGate!({ tool: ASK_USER_QUESTION, questions: colliding, toolUseId: 'toolu_c', signal: new AbortController().signal }))
          yield result(run)
        })()
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: askingColliding })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      await turn!.done
      expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/not asked|could not be shown/) }])
      expect(await db.sql`SELECT request_id FROM ai_pending_input WHERE session_id = ${session.id}`).toEqual([])
      expect((await events(m, session.id)).map((e) => e.type)).not.toContain('question.asked')
      expect((await m.get(session.id, browser)).status).toBe('idle')
    })

    it('the chat socket names the question on a malformed answer, and nothing on another malformed frame', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking })
      const out: ServerEvent[] = []
      const chat = new ChatConnection(m, (e) => out.push(e), { snapshotMs: 60_000 })
      try {
        await chat.receive(JSON.stringify(socketAnswer('s1', 'q1', 'Red')))
        await chat.receive(JSON.stringify({ v: PROTOCOL_VERSION, type: 'session.send', sessionId: 's1', id: 'q1' }))
        await expect.poll(() => out.filter((e) => e.type === 'error').length).toBe(2)
        const [first, second] = out.filter((e) => e.type === 'error')
        expect(first).toMatchObject({ code: 'invalid', sessionId: 's1', questionId: 'q1' })
        expect(second).toMatchObject({ code: 'invalid' })
        expect(second).not.toHaveProperty('questionId')
        expect(second).not.toHaveProperty('sessionId')
      } finally {
        chat.close()
      }
    })

    it('an answer given through another replica reaches the parked turn', async () => {
      const paths = await tempPaths()
      const a = manager({ sql: db.sql, paths, run: asking })
      const b = manager({ sql: db.sql, paths, run: asking })
      const { session, turn } = await a.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(a, session.id)

      await respond(b, browser, id, answerBody(['Blue', 'Approve']))
      await turn!.done
      expect(verdicts[0]).toMatchObject({ answered: true })
    })

    // spec 2026-10-01 §6.6 Notifications: one input.requested and one input.resolved per entry.
    it('logs input.requested after question.asked, and input.resolved after its resolution', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
      const id = await pendingQuestion(m, session.id)
      await respond(m, browser, id, answerBody(['Blue', 'Approve']))
      await turn!.done
      const log = await events(m, session.id)
      const types = log.map((e) => e.type)
      expect(types.indexOf('input.requested')).toBe(types.indexOf('question.asked') + 1)
      expect(types.indexOf('input.resolved')).toBe(types.indexOf('question.resolved') + 1)
      expect(log.filter((e) => e.type.startsWith('input.'))).toEqual([
        expect.objectContaining({
          type: 'input.requested',
          entry: expect.objectContaining({ id, kind: 'answer', prompt: QUESTIONS.map((q) => q.question).join('\n'), expires_at: expect.any(String) }),
        }),
        { v: PROTOCOL_VERSION, type: 'input.resolved', sessionId: session.id, id, kind: 'answer', outcome: 'answered' },
      ])
    })

    // spec 2026-10-01 §6.6 Timeouts: a question's timer cancels it, never answers it.
    describe('question expiry', () => {
      const expiry = (seconds: unknown) => ({ get: async <T,>(key: string) => (key === 'question_expiry_seconds' ? (seconds as T) : undefined) })

      it('bounds question_expiry_seconds to 10–86 400, default 3600', async () => {
        const at = (v: unknown) => new QuestionService({ sql: db.sql, events: {} as EventLog, settings: expiry(v) }).expirySeconds()
        expect(await at(undefined)).toBe(3600)
        expect(await at(5)).toBe(10)
        expect(await at(100_000)).toBe(86_400)
        expect(await at('soon')).toBe(3600)
      })

      it('a parked question whose timer fires is not answered, and the call is told nobody answered', async () => {
        // The gate reads its timers from ai_settings as each turn starts (temporal/session/gateActivities.ts).
        await db.sql`INSERT INTO ai_settings (key, value) VALUES ('question_expiry_seconds', ${db.sql.json(10)})`
        const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking })
        const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
        const id = await pendingQuestion(m, session.id)
        const [row] = await db.sql<{ ttl: number }[]>`
          SELECT round(extract(epoch FROM expires_at - created_at))::int AS ttl FROM ai_pending_input WHERE request_id = ${id}`
        expect(row?.ttl).toBe(10)
        await turn!.done
        expect(verdicts[0]).toMatchObject({ answered: false })
        expect((verdicts[0] as { message: string }).message).toMatch(/did not answer/)
        const after = await responseOf(id)
        expect(after?.response).toBeNull()
        expect(after?.outcome).not.toBe('answered')
        expect((await events(m, session.id)).filter((e) => e.type === 'question.resolved')).toHaveLength(1)
        // An answer after the timer is refused.
        expect(await refused(respond(m, browser, id, answerBody(['Blue', 'Approve'])))).not.toBe('answered')
      }, 40_000)
    })

    // #5053: the rate lock is tried and tried again, never waited on; a holder that never lets go
    // gives the model a refusal past the bound, not a call that never returns.
    it('an attention request whose rate lock is never free is refused past the bound, and opens nothing', async () => {
      const holder = await db.sql.reserve()
      await holder`SELECT pg_advisory_lock(hashtextextended(${ATTENTION_RATE_LOCK}, 0))`
      try {
        const service = new QuestionService({ sql: db.sql, events: {} as EventLog, rateLockWaitMs: 200 })
        let opened = 0
        const started = performance.now()
        const refused = await service.attentionLimit(
          { sessionId: '00000000-0000-4000-8000-000000000001', turnId: '00000000-0000-4000-8000-000000000002', toolUseId: 'toolu_a', tool: 'request_user_attention', reason: 'blocked', own: true },
          async () => {
            opened += 1
          },
        )
        expect(refused).toMatch(/^The user was not asked: /)
        expect(opened).toBe(0)
        expect(performance.now() - started).toBeLessThan(5_000)
      } finally {
        await holder`SELECT pg_advisory_unlock_all()`
        holder.release()
      }
    })
  },
)
