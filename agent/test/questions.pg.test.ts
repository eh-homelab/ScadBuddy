import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { ASK_USER_QUESTION, ASK_USER_TOOL, type QuestionVerdict, type UserQuestion } from '../src/harness/questions.js'
import type { HarnessRun } from '../src/harness/run.js'
import { QuestionError, QuestionService } from '../src/questions/service.js'
import { ChatConnection } from '../src/routes/chat.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { PROTOCOL_VERSION, type ServerEvent } from '../src/sessions/protocol.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, tempPaths } from './support/sessions.js'

// The question store and its rules without the SDK (#940): ai_questions in
// Postgres, the session's `waiting_input`, who may answer, and that a question
// never outlives its turn. The SDK side is test/questions.sdk.test.ts.

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

const answer = (sessionId: string, id: string, answers: string[]) =>
  ({ v: PROTOCOL_VERSION, type: 'question.answer', sessionId, id, answers }) as const

describe.skipIf(!TEST_DATABASE_URL)(`questions in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
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
  const askingWith = (tool: string) => (run: HarnessRun): AsyncIterable<SDKMessage> => {
    runs.push(run)
    return (async function* () {
      await Promise.resolve()
      if (run.questionGate) {
        verdicts.push(await run.questionGate({ tool, questions: QUESTIONS, toolUseId: 'toolu_q1', signal: new AbortController().signal }))
      }
      yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
    })()
  }
  const asking = askingWith(ASK_USER_QUESTION)

  async function events(m: SessionManager, sessionId: string): Promise<ServerEvent[]> {
    return (await m.events.read(sessionId)).map((e) => e.event)
  }

  async function pendingQuestion(m: SessionManager, sessionId: string): Promise<string> {
    let id: string | undefined
    await expect.poll(async () => {
      const [row] = await db.sql<{ id: string }[]>`
        SELECT id FROM ai_questions WHERE session_id = ${sessionId} AND outcome IS NULL`
      id = row?.id
      return id
    }).toBeDefined()
    await expect.poll(async () => (await m.get(sessionId, browser)).status).toBe('waiting_input')
    return id!
  }

  it("parks the turn in waiting_input until the user answers, and hands the answers back keyed by question", async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)

    await m.questions.answer(browser, answer(session.id, id, ['Blue', 'Approve']))
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
    const statuses = log.flatMap((e) => (e.type === 'session.status' ? [e.status] : []))
    expect(statuses).toEqual(['idle', 'running', 'waiting_input', 'running', 'idle'])
    await expectPanelAccepts(log)

    const [row] = await db.sql`SELECT outcome, answers, answered_by_kind FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ outcome: 'answered', answers: ['Blue', 'Approve'], answered_by_kind: 'browser' })
  })

  // #1075: the answer is in the AI audit log, tied to the tool call, as a
  // hash: an answer can be free text the user typed.
  // Which tool asked is recorded (ask_user is a subagent's way, but not only a subagent's).
  it.each([ASK_USER_QUESTION, ASK_USER_TOOL])("records the answer to %s in the audit log: who answered, which call and tool, a hash, never the text", async (tool) => {
    const audit = new AuditLog({ sql: db.sql })
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: askingWith(tool), approvalPollMs: 20, audit })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)
    await m.questions.answer(browser, answer(session.id, id, ['Make it teal, like my car', 'Approve']), { clientIp: '192.0.2.7' })
    await turn!.done

    const rows = await db.sql`
      SELECT action, surface, client_ip, principal_kind, principal_id, session_id, turn_id, tool_use_id, tier,
             input_hash, input_summary, outcome, detail
      FROM ai_audit WHERE kind = 'question'`
    expect(rows).toEqual([
      {
        action: 'answered',
        surface: 'http',
        client_ip: '192.0.2.7',
        principal_kind: 'browser',
        principal_id: browser.id,
        session_id: session.id,
        turn_id: expect.any(String),
        tool_use_id: 'toolu_q1',
        tier: 'read',
        input_hash: audit.hash(tool, { answers: ['Make it teal, like my car', 'Approve'] }),
        input_summary: null,
        outcome: 'ok',
        detail: `${tool} question ${id}: 2 answers`,
      },
    ])
    const [q] = await db.sql<{ turn_id: string; tool: string }[]>`SELECT turn_id, tool FROM ai_questions WHERE id = ${id}`
    expect(q?.tool).toBe(tool)
    expect(rows[0]?.turn_id).toBe(q?.turn_id)
    expect(JSON.stringify(rows)).not.toContain('teal')
  })

  it('only the user in the panel answers, once, with one answer per question', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)

    const refused = (p: Promise<void>) => p.then(() => 'answered', (err: unknown) => (err instanceof QuestionError ? err.code : String(err)))
    expect(await refused(m.questions.answer(agentA, answer(session.id, id, ['Red', 'Cancel'])))).toBe('forbidden')
    expect(await refused(m.questions.answer(browser, answer(session.id, id, ['Red'])))).toBe('invalid')
    expect(await refused(m.questions.answer(browser, answer(session.id, id, ['Red', '  '])))).toBe('invalid')
    expect(await refused(m.questions.answer(browser, answer(session.id, '00000000-0000-4000-8000-000000000000', ['Red', 'Cancel'])))).toBe('not_found')
    expect(await refused(m.questions.answer(browser, answer('00000000-0000-4000-8000-000000000000', id, ['Red', 'Cancel'])))).toBe('not_found')

    await m.questions.answer(browser, answer(session.id, id, ['Red', 'Cancel']))
    expect(await refused(m.questions.answer(browser, answer(session.id, id, ['Blue', 'Approve'])))).toBe('conflict')
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
    const audit = new AuditLog({ sql: db.sql })
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking, approvalPollMs: 20, audit })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)

    expect(await m.interrupt(session.id, browser)).toBe(true)
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/did not answer/) }])
    const [row] = await db.sql`SELECT outcome, answers FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ outcome: 'cancelled', answers: null })
    const resolved = (await events(m, session.id)).find((e) => e.type === 'question.resolved')
    expect(resolved).toMatchObject({ id, answered: false, reason: expect.stringMatching(/interrupted/) })
    expect(resolved).not.toHaveProperty('answers')
    expect((await m.get(session.id, browser)).status).toBe('idle')
    // #1075: a question nobody answered is in the audit log too, as ScadBuddy's cancellation.
    expect(await db.sql`SELECT action, surface, principal_kind, tool_use_id, outcome, detail FROM ai_audit WHERE kind = 'question'`).toEqual([
      {
        action: 'cancelled',
        surface: 'system',
        principal_kind: 'system',
        tool_use_id: 'toolu_q1',
        outcome: 'refused',
        detail: expect.stringMatching(new RegExp(`^AskUserQuestion question ${id}: .*interrupted`)),
      },
    ])
  })

  it('a question and an approval pending together: deciding the approval leaves the session waiting for the answer', async () => {
    const both = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const signal = new AbortController().signal
        const asked = run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q', signal })
        await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(1)
        await run.approvalGate!({ toolName: 'mcp__stub__print', input: { job: 'box' }, toolUseId: 'toolu_p', tier: 'outward', signal })
        verdicts.push(await asked)
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: both, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask and print' })
    let approvalId: string | undefined
    await expect.poll(async () => {
      approvalId = (await m.approvals.list(browser, { sessionId: session.id, pending: true }))[0]?.id
      return approvalId
    }).toBeDefined()
    expect((await m.get(session.id, browser)).status).toBe('waiting_approval')

    await m.approvals.decide(browser, approvalId!, true)
    await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_input')
    const [q] = await db.sql<{ id: string }[]>`SELECT id FROM ai_questions WHERE session_id = ${session.id}`
    await m.questions.answer(browser, answer(session.id, q!.id, ['Red', 'Approve']))
    await turn!.done
    expect(verdicts[0]).toMatchObject({ answered: true })
    const statuses = (await events(m, session.id)).flatMap((e) => (e.type === 'session.status' ? [e.status] : []))
    // The approval's resolution hands back to the question, never straight to running.
    const resolved = statuses.lastIndexOf('waiting_approval')
    expect(statuses[resolved + 1]).toBe('waiting_input')
    expect(statuses.at(-1)).toBe('idle')
  })

  it('a question and an approval pending together: answering the question leaves the session waiting for the approval', async () => {
    const both = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const signal = new AbortController().signal
        const asked = run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q', signal })
        await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(1)
        const approved = run.approvalGate!({ toolName: 'mcp__stub__print', input: { job: 'box' }, toolUseId: 'toolu_p', tier: 'outward', signal })
        verdicts.push(await asked)
        await approved
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: both, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask and print' })
    let approvalId: string | undefined
    await expect.poll(async () => {
      approvalId = (await m.approvals.list(browser, { sessionId: session.id, pending: true }))[0]?.id
      return approvalId
    }).toBeDefined()
    await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_approval')

    const [q] = await db.sql<{ id: string }[]>`SELECT id FROM ai_questions WHERE session_id = ${session.id}`
    await m.questions.answer(browser, answer(session.id, q!.id, ['Red', 'Approve']))
    await expect.poll(() => verdicts.length).toBe(1)
    // The approval is still pending, so the session still says so.
    expect((await m.get(session.id, browser)).status).toBe('waiting_approval')
    await m.approvals.decide(browser, approvalId!, true)
    await turn!.done
    expect((await m.get(session.id, browser)).status).toBe('idle')
  })

  it('an approval asked first, then a question: the session shows the question, then hands back to the approval', async () => {
    const both = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const signal = new AbortController().signal
        const approved = run.approvalGate!({ toolName: 'mcp__stub__print', input: { job: 'box' }, toolUseId: 'toolu_p', tier: 'outward', signal })
        await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_approvals WHERE decision IS NULL`).length).toBe(1)
        verdicts.push(await run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q', signal }))
        await approved
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: both, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'print and ask' })
    const id = await pendingQuestion(m, session.id)

    await m.questions.answer(browser, answer(session.id, id, ['Blue', 'Approve']))
    await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_approval')
    const approvalId = (await m.approvals.list(browser, { sessionId: session.id, pending: true }))[0]!.id
    await m.approvals.decide(browser, approvalId, true)
    await turn!.done
    expect(verdicts[0]).toMatchObject({ answered: true })
    const statuses = (await events(m, session.id)).flatMap((e) => (e.type === 'session.status' ? [e.status] : []))
    expect(statuses.indexOf('waiting_input')).toBeGreaterThan(statuses.indexOf('waiting_approval'))
    expect(statuses.at(-1)).toBe('idle')
  })

  it('a shutdown cancels a pending question (unlike an approval, it does not outlive its turn)', async () => {
    const parked = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        verdicts.push(await run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q', signal: new AbortController().signal }))
        yield* []
        throw new Error('Claude Code process aborted by user')
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: parked, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)

    m.abortAll()
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/did not answer/) }])
    const [row] = await db.sql`SELECT outcome FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ outcome: 'cancelled' })
    expect(await m.get(session.id, browser)).toMatchObject({ status: 'idle', turnActive: false })
  })

  it('cancelling for a turn leaves another turn’s question alone', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)
    // A turn that lost its claim finishing late cancels only its own questions.
    expect(await m.questions.cancelPending(session.id, 'an older turn ended', { turnId: '00000000-0000-4000-8000-000000000001' })).toBe(0)
    expect((await db.sql`SELECT outcome FROM ai_questions WHERE id = ${id}`)[0]).toEqual({ outcome: null })
    await m.questions.answer(browser, answer(session.id, id, ['Red', 'Cancel']))
    await turn!.done
    expect(verdicts[0]).toMatchObject({ answered: true })
  })

  it('the reaper cancels the dead turn’s question and says so', async () => {
    const paths = await tempPaths()
    // A replica whose turn stops renewing its lease (its process is stuck), and another that reaps it.
    const stuck = manager({ sql: db.sql, paths, run: asking, approvalPollMs: 20, renewMs: 600_000 })
    const reaper = manager({ sql: db.sql, paths, run: asking, approvalPollMs: 20 })
    const { session, turn } = await stuck.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(stuck, session.id)
    await db.sql`UPDATE ai_sessions SET lease_until = now() - interval '1 second' WHERE id = ${session.id}`

    expect(await reaper.reapExpired()).toEqual([session.id])
    expect((await db.sql`SELECT outcome FROM ai_questions WHERE id = ${id}`)[0]).toEqual({ outcome: 'cancelled' })
    const resolved = (await events(reaper, session.id)).find((e) => e.type === 'question.resolved')
    expect(resolved).toMatchObject({ id, answered: false, reason: 'the turn ended' })
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/did not answer/) }])
  })

  it('a handoff cancels the pending question, and the running turn cannot park on another for the new owner', async () => {
    let second: QuestionVerdict | undefined
    const twice = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const signal = new AbortController().signal
        verdicts.push(await run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q1', signal }))
        second = await run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_q2', signal })
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: twice, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    await pendingQuestion(m, session.id)

    await m.handoff(session.id, browser, agentA)
    await m.acceptHandoff(session.id, agentA)
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/handed off/) }])
    expect(second).toEqual({ answered: false, message: expect.stringMatching(/no longer the user/) })
    // Nothing was recorded for the second call: no card, no wait.
    expect(await db.sql`SELECT tool_use_id, outcome FROM ai_questions WHERE session_id = ${session.id}`).toEqual([
      { tool_use_id: 'toolu_q1', outcome: 'cancelled' },
    ])
    expect((await m.get(session.id, agentA)).status).toBe('idle')
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
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: dropped, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)

    withdraw.abort()
    await expect.poll(() => verdicts.length).toBe(1)
    expect(verdicts[0]).toMatchObject({ answered: false })
    expect((await db.sql`SELECT outcome, reason FROM ai_questions WHERE id = ${id}`)[0]).toEqual({
      outcome: 'cancelled',
      reason: 'the call was withdrawn',
    })
    // The turn is still live, and no longer says it waits for the user.
    expect(await m.get(session.id, browser)).toMatchObject({ status: 'running', turnActive: true })
    expect((await events(m, session.id)).find((e) => e.type === 'question.resolved')).toMatchObject({ id, answered: false })
    carryOn()
    await turn!.done
  })

  // #1075: a cancellation audits every question it ends, and an audit sink that
  // fails costs neither the other rows nor the session's status.
  it('cancelling two questions audits each, and a failing audit write costs neither the other row nor the status', async () => {
    const real = new AuditLog({ sql: db.sql })
    let failures = 1
    const audit = {
      hash: real.hash.bind(real),
      record: (entry: Parameters<AuditLog['record']>[0]) => (failures-- > 0 ? Promise.reject(new Error('audit down')) : real.record(entry)),
    }
    const twice = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const signal = new AbortController().signal
        const second = { ...QUESTIONS[0]!, question: 'And the top?' }
        verdicts.push(
          ...(await Promise.all([
            run.questionGate!({ tool: ASK_USER_QUESTION, questions: QUESTIONS, toolUseId: 'toolu_a', signal }),
            run.questionGate!({ tool: ASK_USER_TOOL, questions: [second], toolUseId: 'toolu_b', signal }),
          ])),
        )
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: twice, approvalPollMs: 20, audit: audit as unknown as AuditLog })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me twice' })
    await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(2)
    await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_input')

    expect(await m.questions.cancelPending(session.id, 'the test cancelled it')).toBe(2)
    // Nothing is pending: the session no longer says it waits (running, or idle once the turn ends).
    expect((await m.get(session.id, browser)).status).not.toBe('waiting_input')
    await turn!.done
    expect(verdicts).toHaveLength(2)
    expect(verdicts.every((v) => !v.answered)).toBe(true)
    // The first write failed; the second question's row still landed.
    const rows = await db.sql<{ tool_use_id: string; detail: string }[]>`
      SELECT tool_use_id, detail FROM ai_audit WHERE kind = 'question' AND action = 'cancelled'`
    expect(rows).toHaveLength(1)
    expect(rows[0]?.detail).toMatch(/the test cancelled it$/)
  })

  it('an answer committed before a failed status refresh is still audited', async () => {
    const audit = new AuditLog({ sql: db.sql })
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking, approvalPollMs: 20, audit })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)
    vi.spyOn(m.questions, 'refreshStatus').mockRejectedValueOnce(new Error('status write failed'))
    await expect(m.questions.answer(browser, answer(session.id, id, ['Red', 'Cancel']))).rejects.toThrow('status write failed')
    await turn!.done
    expect(await db.sql`SELECT action, outcome FROM ai_audit WHERE kind = 'question'`).toEqual([{ action: 'answered', outcome: 'ok' }])
  })

  it('an answer given over the chat socket is audited with the socket client address', async () => {
    const audit = new AuditLog({ sql: db.sql })
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking, approvalPollMs: 20, audit })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)
    const chat = new ChatConnection(m, () => {}, { snapshotMs: 60_000, clientIp: '198.51.100.4' })
    try {
      await chat.receive(JSON.stringify(answer(session.id, id, ['Red', 'Cancel'])))
      await turn!.done
    } finally {
      chat.close()
    }
    expect(await db.sql`SELECT surface, client_ip, outcome FROM ai_audit WHERE kind = 'question'`).toEqual([
      { surface: 'http', client_ip: '198.51.100.4', outcome: 'ok' },
    ])
  })

  it('the chat socket names the question on an error that refused its answer', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)
    const out: ServerEvent[] = []
    const chat = new ChatConnection(m, (e) => out.push(e), { snapshotMs: 60_000 })
    try {
      await chat.receive(JSON.stringify(answer(session.id, id, ['Red'])))
      await expect.poll(() => out.find((e) => e.type === 'error')).toMatchObject({ sessionId: session.id, code: 'invalid', questionId: id })
    } finally {
      chat.close()
    }
    await m.questions.answer(browser, answer(session.id, id, ['Red', 'Cancel']))
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
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: askingColliding, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/could not be shown/) }])
    expect(await db.sql`SELECT id FROM ai_questions WHERE session_id = ${session.id}`).toEqual([])
    expect((await events(m, session.id)).map((e) => e.type)).not.toContain('question.asked')
    expect((await m.get(session.id, browser)).status).toBe('idle')
  })

  it('the chat socket names the question on a malformed answer, and nothing on another malformed frame', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking, approvalPollMs: 20 })
    const out: ServerEvent[] = []
    const chat = new ChatConnection(m, (e) => out.push(e), { snapshotMs: 60_000 })
    try {
      await chat.receive(JSON.stringify({ v: PROTOCOL_VERSION, type: 'question.answer', sessionId: 's1', id: 'q1', answers: 'Red' }))
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

  it('a row and the event that reports it commit together: a failed append resolves nothing', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: asking, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(m, session.id)
    const failing = { append: () => Promise.reject(new Error('the log is down')), committed: () => {} } as unknown as EventLog
    const broken = new QuestionService({ sql: db.sql, events: failing })

    await expect(broken.answer(browser, answer(session.id, id, ['Red', 'Cancel']))).rejects.toThrow('the log is down')
    await expect(broken.cancelPending(session.id, 'x')).rejects.toThrow('the log is down')
    const [row] = await db.sql`SELECT outcome FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ outcome: null })

    // Still answerable through the working log.
    await m.questions.answer(browser, answer(session.id, id, ['Red', 'Cancel']))
    await turn!.done
    expect(verdicts[0]).toMatchObject({ answered: true })
  })

  it('an answer given through another replica reaches the parked turn', async () => {
    const paths = await tempPaths()
    const a = manager({ sql: db.sql, paths, run: asking, approvalPollMs: 20 })
    const b = manager({ sql: db.sql, paths, run: asking, approvalPollMs: 20 })
    const { session, turn } = await a.start(browser, { origin: 'chat', prompt: 'ask me' })
    const id = await pendingQuestion(a, session.id)

    await b.questions.answer(browser, answer(session.id, id, ['Blue', 'Approve']))
    await turn!.done
    expect(verdicts[0]).toMatchObject({ answered: true })
  })
})
