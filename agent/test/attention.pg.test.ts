import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp } from '../src/app.js'
import { AuditLog } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { attentionCard, type AttentionSpec, parseAttention, timedOutText } from '../src/harness/attention.js'
import { ATTENTION_TOOL, type QuestionGate, type QuestionRequest, type QuestionVerdict } from '../src/harness/questions.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { ATTENTION_RATE_LIMIT, PENDING_CAP } from '../src/questions/service.js'
import { pendingInput } from '../src/routes/pendingInput.js'
import { type SessionManager, TAB_WAIT_S, TAB_WAITS_PER_TURN, waitForTab } from '../src/sessions/manager.js'
import type { TabWait, WaitForTab } from '../src/tools/registry.js'
import { PROTOCOL_VERSION, type ServerEvent } from '../src/sessions/protocol.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, tempPaths } from './support/sessions.js'

// Attention requests (#815) at the question gate, in Postgres: parked as an
// `ai_questions` row of kind 'attention', answered like a question, and timed
// out by #815's rule, which never answers. The SDK side is
// test/attention.sdk.test.ts.

const input = (over: Record<string, unknown> = {}) => {
  const parsed = parseAttention({ reason: 'tab_disconnected', message: 'The ScadBuddy tab closed; reopen it?', ...over })
  if (!parsed.ok) throw new Error(parsed.error)
  return parsed.input
}

const answer = (sessionId: string, id: string, answers: string[]) =>
  ({ v: PROTOCOL_VERSION, type: 'question.answer', sessionId, id, answers }) as const

describe.skipIf(!TEST_DATABASE_URL)(`attention requests in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let verdicts: QuestionVerdict[]

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    verdicts = []
  })
  afterEach(async () => {
    await drop()
  })

  /** A turn that raises each request in `requests` through its gate, one after another, then ends. */
  const raising = (...requests: { spec: AttentionSpec; message?: string }[]) => (run: HarnessRun): AsyncIterable<SDKMessage> =>
    (async function* () {
      await Promise.resolve()
      for (const [i, r] of requests.entries()) {
        const card = attentionCard(input({ message: r.message ?? 'The ScadBuddy tab closed; reopen it?' }))
        verdicts.push(
          await run.questionGate!({
            tool: ATTENTION_TOOL,
            questions: [card],
            toolUseId: `toolu_a${i}`,
            signal: new AbortController().signal,
            attention: r.spec,
          }),
        )
      }
      // As the SDK does: an aborted query ends without a result.
      if (run.signal?.aborted) return
      yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
    })()

  const spec = (over: Partial<AttentionSpec> = {}): AttentionSpec => ({ reason: 'tab_disconnected', onTimeout: 'proceed', timeoutS: 300, ...over })

  async function events(m: SessionManager, sessionId: string): Promise<ServerEvent[]> {
    return (await m.events.read(sessionId)).map((e) => e.event)
  }

  async function pending(sessionId: string): Promise<string> {
    let id: string | undefined
    await expect.poll(async () => {
      const [row] = await db.sql<{ id: string }[]>`SELECT id FROM ai_questions WHERE session_id = ${sessionId} AND outcome IS NULL`
      id = row?.id
      return id
    }).toBeDefined()
    return id!
  }

  it('parks the turn in waiting_input as an attention card, and the user acknowledges it with a reply', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec() }), approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'render it in my tab' })
    const id = await pending(session.id)
    await expect.poll(async () => (await m.get(session.id, browser)).status).toBe('waiting_input')

    const [row] = await db.sql`
      SELECT kind, attention_reason, on_timeout, tool, extract(epoch FROM expires_at - created_at)::int AS window
      FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ kind: 'attention', attention_reason: 'tab_disconnected', on_timeout: 'proceed', tool: ATTENTION_TOOL, window: 300 })

    await m.questions.answer(browser, answer(session.id, id, ["I'm here"]))
    await turn!.done
    expect(verdicts).toEqual([{ answered: true, answers: { 'The ScadBuddy tab closed; reopen it?': "I'm here" } }])

    const log = await events(m, session.id)
    const asked = log.find((e) => e.type === 'question.asked')
    expect(asked).toMatchObject({
      id,
      tool: 'toolu_a0',
      questions: [{ question: 'The ScadBuddy tab closed; reopen it?', header: 'Tab disconnected', options: [{ label: "I'm here" }, { label: 'Carry on without me' }] }],
      attention: { reason: 'tab_disconnected', onTimeout: 'proceed', expiresAt: expect.any(String) },
    })
    await expectPanelAccepts(log)
  })

  it("is listed as pending for the badge while it waits, with its timer, and not once it is answered", async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec({ reason: 'blocked' }) }), approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const id = await pending(session.id)
    expect(await m.questions.listPending()).toEqual([
      expect.objectContaining({ id, sessionId: session.id, kind: 'attention', attentionReason: 'blocked', onTimeout: 'proceed', expiresAt: expect.any(String) }),
    ])
    await m.questions.answer(browser, answer(session.id, id, ['Carry on without me']))
    await turn!.done
    expect(await m.questions.listPending()).toEqual([])
  })

  // #815 §4: the timer never answers for the user, and never approves.
  it("on_timeout 'proceed': the timer resolves it as timed_out, never answered, and the turn goes on", async () => {
    const audit = new AuditLog({ sql: db.sql })
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec({ timeoutS: 0.3 }) }), approvalPollMs: 20, audit })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const id = await pending(session.id)
    await turn!.done

    expect(verdicts).toEqual([{ answered: false, timedOut: true, message: expect.stringMatching(/nobody replied/) }])
    const [row] = await db.sql`SELECT outcome, answers, answered_by_kind FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ outcome: 'timed_out', answers: null, answered_by_kind: null })
    const log = await events(m, session.id)
    const resolved = log.find((e) => e.type === 'question.resolved')
    expect(resolved).toMatchObject({ id, answered: false, reason: expect.stringMatching(/proceed/) })
    expect(resolved).not.toHaveProperty('answers')
    expect(resolved).not.toHaveProperty('by')
    // The turn finished on its own: not interrupted.
    expect(log.filter((e) => e.type === 'error')).toEqual([])
    expect((await m.get(session.id, browser)).status).toBe('idle')
    await expectPanelAccepts(log)
    expect(await db.sql`SELECT action, surface, principal_kind, outcome FROM ai_audit WHERE kind = 'question'`).toEqual([
      { action: 'timed_out', surface: 'system', principal_kind: 'system', outcome: 'refused' },
    ])
  })

  it("on_timeout 'stop': the timer ends the turn as an interrupt does", async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec({ onTimeout: 'stop', timeoutS: 0.3 }) }), approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const id = await pending(session.id)
    await turn!.done

    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/did not reply.*on_timeout: stop.*turn ends/) }])
    const [row] = await db.sql`SELECT outcome FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ outcome: 'timed_out' })
    const log = await events(m, session.id)
    expect(log.find((e) => e.type === 'question.resolved')).toMatchObject({ id, answered: false, reason: expect.stringMatching(/on_timeout: stop/) })
    expect(log.find((e) => e.type === 'error')).toMatchObject({ code: 'interrupted' })
    expect((await m.get(session.id, browser)).status).toBe('idle')
  })

  it('an interrupt cancels it like a question: nothing is answered and nothing times out', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec() }), approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const id = await pending(session.id)
    expect(await m.interrupt(session.id, browser)).toBe(true)
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/did not answer/) }])
    expect((await db.sql`SELECT outcome FROM ai_questions WHERE id = ${id}`)[0]).toEqual({ outcome: 'cancelled' })
  })

  // #815 §5: one open request per session per reason; a new one replaces the last.
  it('a second request for the same reason supersedes the open one; another reason does not', async () => {
    const supersedes = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const ask = (toolUseId: string, s: AttentionSpec) =>
          run.questionGate!({ tool: ATTENTION_TOOL, questions: [attentionCard(input())], toolUseId, signal: new AbortController().signal, attention: s })
        const first = ask('toolu_1', spec())
        await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(1)
        const other = ask('toolu_2', spec({ reason: 'blocked', timeoutS: 0.3 }))
        await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(2)
        const second = ask('toolu_3', spec({ timeoutS: 0.3 }))
        verdicts.push(await first, await other, await second)
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: supersedes, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await turn!.done
    expect(verdicts).toEqual([
      { answered: false, message: expect.stringMatching(/replaced by a newer request/) },
      { answered: false, timedOut: true, message: expect.any(String) },
      { answered: false, timedOut: true, message: expect.any(String) },
    ])
    const rows = await db.sql`SELECT tool_use_id, outcome FROM ai_questions WHERE session_id = ${session.id} ORDER BY created_at`
    expect(rows).toEqual([
      { tool_use_id: 'toolu_1', outcome: 'cancelled' },
      { tool_use_id: 'toolu_2', outcome: 'timed_out' },
      { tool_use_id: 'toolu_3', outcome: 'timed_out' },
    ])
  })

  // #815 §4: the done summary is posted, never waited on, and outlives its turn.
  const DONE = 'Rendered the sign headlessly; the plate still needs your tab.'
  const touch = (sessionId: string, id: string) => db.sql`
    INSERT INTO ai_session_resources (session_id, tool, resource_type, resource_id, action, model_slug)
    VALUES (${sessionId}, 'mcp__scadbuddy__save_preset', 'preset', ${id}, 'created', 'sign')`
  const postDone = (run: HarnessRun, toolUseId = 'toolu_done') =>
    run.questionGate!({
      tool: ATTENTION_TOOL,
      questions: [attentionCard(input({ reason: 'done', message: DONE }))],
      toolUseId,
      signal: new AbortController().signal,
      attention: { reason: 'done' },
    })
  const result = (run: HarnessRun) =>
    ({ type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume }) as unknown as SDKMessage

  it('a done summary returns at once, outlives its turn on the badge with what the turn touched, and is dismissed', async () => {
    const working = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        await touch(run.sessionId ?? run.resume!, 'preset-1')
        verdicts.push(await postDone(run))
        yield result(run)
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: working, approvalPollMs: 20 })
    const { session: earlier } = await m.start(browser, { origin: 'chat', title: 'earlier' })
    // Another session's touch is not this turn's.
    await touch(earlier.id, 'not-mine')
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'render it' })
    await turn!.done

    expect(verdicts).toEqual([{ answered: false, posted: true, message: 'posted' }])
    const [row] = await db.sql`SELECT id, kind, attention_reason, on_timeout, expires_at, outcome, summary FROM ai_questions WHERE session_id = ${session.id}`
    expect(row).toMatchObject({ kind: 'attention', attention_reason: 'done', on_timeout: null, expires_at: null, outcome: null })
    const summary = '**What this turn changed**\n- created preset `preset-1` of `sign` (`save_preset`)'
    expect(row!.summary).toBe(summary)
    // Nothing waits on it: the session is idle, not waiting_input.
    expect((await m.get(session.id, browser)).status).toBe('idle')
    const log = await events(m, session.id)
    expect(log.find((e) => e.type === 'question.asked')).toMatchObject({ attention: { reason: 'done', summary } })
    expect(log.filter((e) => e.type === 'question.resolved')).toEqual([])
    await expectPanelAccepts(log)

    expect(await pendingInput(m)).toEqual([
      expect.objectContaining({ id: `question:${row!.id}`, kind: 'answer', prompt: DONE, expires_at: null, attention: { reason: 'done', on_timeout: null, summary } }),
    ])
    await m.questions.answer(browser, answer(session.id, row!.id as string, ['Dismiss']))
    expect(await pendingInput(m)).toEqual([])
    expect((await m.get(session.id, browser)).status).toBe('idle')
  })

  it("a done summary lists what was done while a request went unanswered first, apart from what came before", async () => {
    const away = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const sessionId = run.sessionId ?? run.resume!
        await touch(sessionId, 'before')
        const card = attentionCard(input())
        verdicts.push(
          await run.questionGate!({ tool: ATTENTION_TOOL, questions: [card], toolUseId: 'toolu_tab', signal: new AbortController().signal, attention: spec({ timeoutS: 0.3 }) }),
        )
        await touch(sessionId, 'unattended')
        verdicts.push(await postDone(run))
        yield result(run)
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: away, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, timedOut: true, message: expect.any(String) }, { answered: false, posted: true, message: 'posted' }])
    const [timedOut] = await db.sql<{ id: string }[]>`SELECT id FROM ai_questions WHERE session_id = ${session.id} AND outcome = 'timed_out'`
    const [done] = await db.sql<{ summary: string }[]>`SELECT summary FROM ai_questions WHERE session_id = ${session.id} AND attention_reason = 'done'`
    expect(done!.summary).toBe(
      `**While nobody answered (attention request ${timedOut!.id.slice(0, 8)} timed out)**\n- created preset \`unattended\` of \`sign\` (\`save_preset\`)` +
        '\n\n**Before you were asked**\n- created preset `before` of `sign` (`save_preset`)',
    )
  })

  it('a second timeout after the user replied opens a second unattended window', async () => {
    const twice = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const ask = (toolUseId: string, s: AttentionSpec) =>
          run.questionGate!({ tool: ATTENTION_TOOL, questions: [attentionCard(input())], toolUseId, signal: new AbortController().signal, attention: s })
        verdicts.push(await ask('toolu_a', spec({ timeoutS: 0.3 })))
        verdicts.push(await ask('toolu_b', spec())) // the user answers this one
        verdicts.push(await ask('toolu_c', spec({ timeoutS: 0.3 })))
        await touch(run.sessionId ?? run.resume!, 'unattended')
        verdicts.push(await postDone(run))
        yield result(run)
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: twice, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    let b: string | undefined
    await expect.poll(async () => {
      const [row] = await db.sql<{ id: string }[]>`
        SELECT id FROM ai_questions WHERE session_id = ${session.id} AND tool_use_id = 'toolu_b' AND outcome IS NULL`
      b = row?.id
      return b
    }).toBeDefined()
    await m.questions.answer(browser, answer(session.id, b!, ["I'm here"]))
    await turn!.done
    const timedOut = await db.sql<{ id: string }[]>`
      SELECT id FROM ai_questions WHERE session_id = ${session.id} AND outcome = 'timed_out' ORDER BY created_at`
    expect(timedOut).toHaveLength(2)
    const [done] = await db.sql<{ summary: string }[]>`SELECT summary FROM ai_questions WHERE session_id = ${session.id} AND attention_reason = 'done'`
    expect(done!.summary).toBe(
      `**While nobody answered (attention requests ${timedOut[0]!.id.slice(0, 8)}, ${timedOut[1]!.id.slice(0, 8)} timed out)**\n` +
        '- created preset `unattended` of `sign` (`save_preset`)',
    )
  })

  it("the summary's turn start is the database's clock, not the agent's", async () => {
    // The agent's clock runs an hour ahead of Postgres's: a touch Postgres stamps during the turn is still this turn's.
    vi.useFakeTimers({ toFake: ['Date'], now: Date.now() + 3_600_000, shouldAdvanceTime: true })
    try {
      const working = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          await Promise.resolve()
          await touch(run.sessionId ?? run.resume!, 'preset-1')
          verdicts.push(await postDone(run))
          yield result(run)
        })()
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: working, approvalPollMs: 20 })
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'render it' })
      await turn!.done
      const [done] = await db.sql<{ summary: string }[]>`SELECT summary FROM ai_questions WHERE session_id = ${session.id} AND attention_reason = 'done'`
      expect(done!.summary).toBe('**What this turn changed**\n- created preset `preset-1` of `sign` (`save_preset`)')
    } finally {
      vi.useRealTimers()
    }
  })

  it('undismissed done summaries do not push a request a turn is parked on off the pending list', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec({ reason: 'blocked' }) }), approvalPollMs: 20 })
    const { session: old } = await m.start(browser, { origin: 'chat', title: 'old' })
    await db.sql`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, summary, created_at)
      SELECT gen_random_uuid(), ${old.id}, gen_random_uuid(), ${ATTENTION_TOOL}, 'toolu_d' || i, '[]', 'attention', 'done', 'x',
             now() - interval '1 day' + i * interval '1 second'
      FROM generate_series(1, ${PENDING_CAP}) AS i`
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const id = await pending(session.id)
    const listed = await m.questions.listPending()
    expect(listed[0]).toMatchObject({ id, attentionReason: 'blocked' })
    expect(listed.filter((q) => q.attentionReason === 'done')).toHaveLength(PENDING_CAP)
    await m.questions.answer(browser, answer(session.id, id, ['Carry on without me']))
    await turn!.done
  })

  it('a timed done row (a replica on an older image) is cancelled when its turn ends; an untimed one is not', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising() })
    const { session } = await m.start(browser, { origin: 'chat', title: 'q' })
    const turnId = '00000000-0000-4000-8000-0000000000aa'
    await db.sql`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, on_timeout, expires_at)
      VALUES (gen_random_uuid(), ${session.id}, ${turnId}, ${ATTENTION_TOOL}, 'toolu_old', '[]', 'attention', 'done', 'proceed', now() + interval '5 minutes')`
    await db.sql`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, summary)
      VALUES (gen_random_uuid(), ${session.id}, ${turnId}, ${ATTENTION_TOOL}, 'toolu_new', '[]', 'attention', 'done', 'x')`
    expect(await m.questions.cancelPending(session.id, 'the turn ended', { turnId })).toBe(1)
    expect(await db.sql`SELECT tool_use_id, outcome FROM ai_questions WHERE session_id = ${session.id} ORDER BY tool_use_id`).toEqual([
      { tool_use_id: 'toolu_new', outcome: null },
      { tool_use_id: 'toolu_old', outcome: 'cancelled' },
    ])
  })

  it("a later turn's done summary does not replace one that recorded unattended actions", async () => {
    let n = 0
    const turns = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        if (n === 0) {
          verdicts.push(
            await run.questionGate!({ tool: ATTENTION_TOOL, questions: [attentionCard(input())], toolUseId: 'toolu_tab', signal: new AbortController().signal, attention: spec({ timeoutS: 0.3 }) }),
          )
          await touch(run.sessionId ?? run.resume!, 'unattended')
        }
        verdicts.push(await postDone(run, `toolu_done${n++}`))
        yield result(run)
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: turns, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'one' })
    await turn!.done
    await (await m.send(session.id, browser, 'two')).done
    const done = (await m.questions.listPending()).filter((q) => q.attentionReason === 'done')
    expect(done.map((q) => q.toolUseId)).toEqual(['toolu_done1', 'toolu_done0'])
    expect(done[1]!.summary).toContain('created preset `unattended`')
    expect(await db.sql`SELECT unattended FROM ai_questions WHERE session_id = ${session.id} AND tool_use_id = 'toolu_done0'`).toEqual([{ unattended: true }])
  })

  it("one turn's repeated done posts keep only its latest, unattended record or not", async () => {
    const repeat = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        verdicts.push(
          await run.questionGate!({ tool: ATTENTION_TOOL, questions: [attentionCard(input())], toolUseId: 'toolu_tab', signal: new AbortController().signal, attention: spec({ timeoutS: 0.3 }) }),
        )
        await touch(run.sessionId ?? run.resume!, 'unattended')
        for (let i = 0; i < 3; i++) verdicts.push(await postDone(run, `toolu_done${i}`))
        yield result(run)
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: repeat, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await turn!.done
    expect(await db.sql`SELECT tool_use_id FROM ai_questions WHERE session_id = ${session.id} AND attention_reason = 'done' AND outcome IS NULL`).toEqual([
      { tool_use_id: 'toolu_done2' },
    ])
  })

  it("a later turn's done replaces one whose unattended window recorded nothing", async () => {
    let n = 0
    const turns = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        if (n === 0) {
          // A request times out, but nothing is touched while nobody answers.
          await touch(run.sessionId ?? run.resume!, 'before')
          verdicts.push(
            await run.questionGate!({ tool: ATTENTION_TOOL, questions: [attentionCard(input())], toolUseId: 'toolu_tab', signal: new AbortController().signal, attention: spec({ timeoutS: 0.3 }) }),
          )
        }
        verdicts.push(await postDone(run, `toolu_done${n++}`))
        yield result(run)
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: turns, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'one' })
    await turn!.done
    const [first] = await db.sql<{ summary: string; unattended: boolean }[]>`
      SELECT summary, unattended FROM ai_questions WHERE session_id = ${session.id} AND tool_use_id = 'toolu_done0'`
    expect(first).toMatchObject({ unattended: false, summary: expect.stringContaining('While nobody answered') })
    await (await m.send(session.id, browser, 'two')).done
    const pendingDone = (await m.questions.listPending()).filter((q) => q.attentionReason === 'done')
    expect(pendingDone.map((q) => q.toolUseId)).toEqual(['toolu_done1'])
  })

  it('whether an earlier done is kept follows its unattended flag, never its summary text', async () => {
    const posting = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        verdicts.push(await postDone(run, 'toolu_new'))
        yield result(run)
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: posting, approvalPollMs: 20 })
    const { session } = await m.start(browser, { origin: 'chat', title: 'q' })
    // One reads like an unattended record but is not flagged; the other is flagged under any heading.
    await db.sql`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, summary, unattended, created_at)
      VALUES (gen_random_uuid(), ${session.id}, gen_random_uuid(), ${ATTENTION_TOOL}, 'toolu_text', '[]', 'attention', 'done',
              '**While nobody answered (attention request 01234567 timed out)**\n- created preset x', false, now() - interval '2 minutes'),
             (gen_random_uuid(), ${session.id}, gen_random_uuid(), ${ATTENTION_TOOL}, 'toolu_flag', '[]', 'attention', 'done',
              '**Some other heading**\n- created preset y', true, now() - interval '1 minute')`
    await (await m.send(session.id, browser, 'go')).done
    expect(await db.sql`SELECT tool_use_id, outcome FROM ai_questions WHERE session_id = ${session.id} ORDER BY created_at`).toEqual([
      { tool_use_id: 'toolu_text', outcome: 'cancelled' },
      { tool_use_id: 'toolu_flag', outcome: null },
      { tool_use_id: 'toolu_new', outcome: null },
    ])
  })

  it('a done summary is neither refused by nor counted in the attention rate limit', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising() })
    const { session: other } = await m.start(browser, { origin: 'chat', title: 'earlier' })
    await db.sql`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, on_timeout, expires_at,
                                outcome, reason, resolved_at)
      SELECT gen_random_uuid(), ${other.id}, gen_random_uuid(), ${ATTENTION_TOOL}, 'toolu_t' || i, '[]', 'attention', 'blocked', 'proceed', now(),
             'timed_out', 'old', now()
      FROM generate_series(1, ${ATTENTION_RATE_LIMIT}) AS i`
    const posting = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        verdicts.push(await postDone(run))
        yield result(run)
      })()
    const p = manager({ sql: db.sql, paths: await tempPaths(), run: posting, approvalPollMs: 20 })
    const { session, turn } = await p.start(browser, { origin: 'chat', prompt: 'go' })
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, posted: true, message: 'posted' }])
    expect(await db.sql`SELECT outcome FROM ai_questions WHERE session_id = ${session.id} AND attention_reason = 'done'`).toEqual([{ outcome: null }])
  })

  it("a later turn's done summary replaces the earlier one; neither is cancelled by its turn ending", async () => {
    let n = 0
    const posting = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        verdicts.push(await postDone(run, `toolu_done${n++}`))
        yield result(run)
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: posting, approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'one' })
    await turn!.done
    const second = await m.send(session.id, browser, 'two')
    await second.done
    const rows = await db.sql`SELECT tool_use_id, outcome, reason FROM ai_questions WHERE session_id = ${session.id} ORDER BY created_at`
    expect(rows).toEqual([
      { tool_use_id: 'toolu_done0', outcome: 'cancelled', reason: 'replaced by a newer request for the same reason' },
      { tool_use_id: 'toolu_done1', outcome: null, reason: null },
    ])
  })

  it('the schema allows a done request without a timer, and no other attention request', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising() })
    const { session } = await m.start(browser, { origin: 'chat', title: 'q' })
    await expect(db.sql`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason)
      VALUES (gen_random_uuid(), ${session.id}, gen_random_uuid(), ${ATTENTION_TOOL}, 'toolu_b', '[]', 'attention', 'blocked')`).rejects.toThrow(
      /ai_questions_attention_check/,
    )
    await expect(db.sql`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, on_timeout, expires_at, summary)
      VALUES (gen_random_uuid(), ${session.id}, gen_random_uuid(), ${ATTENTION_TOOL}, 'toolu_s', '[]', 'attention', 'blocked', 'proceed', now(), 'x')`).rejects.toThrow(
      /ai_questions_summary_check/,
    )
  })

  it('refuses past the per-user rate limit without parking', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec() }), approvalPollMs: 20 })
    const { session: other } = await m.start(browser, { origin: 'chat', title: 'earlier' })
    for (let i = 0; i < ATTENTION_RATE_LIMIT; i++) {
      await db.sql`
        INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, on_timeout,
                                  expires_at, outcome, reason, resolved_at)
        VALUES (gen_random_uuid(), ${other.id}, gen_random_uuid(), ${ATTENTION_TOOL}, ${`toolu_old${i}`}, '[]',
                'attention', 'done', 'proceed', now(), 'timed_out', 'old', now())`
    }
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await turn!.done
    expect(verdicts).toEqual([{ answered: false, message: expect.stringMatching(/at most 10 attention requests/) }])
    expect(await db.sql`SELECT 1 FROM ai_questions WHERE session_id = ${session.id}`).toEqual([])
  })

  it('holds the rate limit across sessions raising at once: one parks, the other is refused', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising({ spec: spec({ timeoutS: 0.3 }) }), approvalPollMs: 20 })
    const { session: other } = await m.start(browser, { origin: 'chat', title: 'earlier' })
    for (let i = 0; i < ATTENTION_RATE_LIMIT - 1; i++) {
      await db.sql`
        INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, on_timeout,
                                  expires_at, outcome, reason, resolved_at)
        VALUES (gen_random_uuid(), ${other.id}, gen_random_uuid(), ${ATTENTION_TOOL}, ${`toolu_old${i}`}, '[]',
                'attention', 'done', 'proceed', now(), 'timed_out', 'old', now())`
    }
    const [a, b] = await Promise.all([
      m.start(browser, { origin: 'chat', prompt: 'one' }),
      m.start(browser, { origin: 'chat', prompt: 'two' }),
    ])
    await Promise.all([a.turn!.done, b.turn!.done])
    expect(verdicts.filter((v) => !v.answered && 'timedOut' in v && v.timedOut)).toHaveLength(1)
    expect(verdicts.filter((v) => !v.answered && /at most 10/.test(v.message))).toHaveLength(1)
    const parked = await db.sql`SELECT 1 FROM ai_questions WHERE session_id IN (${a.session.id}, ${b.session.id})`
    expect(parked).toHaveLength(1)
  })

  it('a question cannot be timed out, by the schema itself', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: raising() })
    const { session } = await m.start(browser, { origin: 'chat', title: 'q' })
    await expect(db.sql`
      INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, outcome, reason, resolved_at)
      VALUES (gen_random_uuid(), ${session.id}, gen_random_uuid(), 'AskUserQuestion', 'toolu_q', '[]', 'timed_out', 'x', now())`).rejects.toThrow(
      /ai_questions_outcome_check/,
    )
  })

  // Spec §6.6's aggregate read, for the badge: every kind in one shape.
  it('GET /api/v1/ai/pending-input lists the parked approval, question and attention request, and only for the UI', async () => {
    const all = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const signal = new AbortController().signal
        const card = attentionCard(input({ reason: 'blocked', message: 'Load the PETG, please' }))
        const att = run.questionGate!({ tool: ATTENTION_TOOL, questions: [card], toolUseId: 'toolu_a', signal, attention: spec({ reason: 'blocked' }) })
        const approved = run.approvalGate!({ toolName: 'mcp__stub__print', input: { job: 'box' }, toolUseId: 'toolu_p', tier: 'outward', signal })
        verdicts.push(await att)
        await approved
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: all, approvalPollMs: 20 })
    const app = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
    })
    const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_approvals WHERE decision IS NULL`).length).toBe(1)
    const attId = await pending(session.id)
    // A question parked in another of the user's sessions: also counted.
    const asker = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        await run.questionGate!({
          tool: 'AskUserQuestion',
          questions: [{ question: 'Which colour?', header: 'Colour', multiSelect: false, options: [{ label: 'Red', description: '' }, { label: 'Blue', description: '' }] }],
          toolUseId: 'toolu_q',
          signal: new AbortController().signal,
        })
        if (run.signal?.aborted) return
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m2 = manager({ sql: db.sql, paths: await tempPaths(), run: asker, approvalPollMs: 20 })
    const { session: s2, turn: t2 } = await m2.start(browser, { origin: 'chat', prompt: 'ask' })
    const qId = await pending(s2.id)

    expect((await app.request('/api/v1/ai/pending-input')).status).toBe(403)
    const response = await app.request('/api/v1/ai/pending-input', { headers: UI_READ })
    expect(response.status).toBe(200)
    const { entries } = (await response.json()) as { entries: Record<string, unknown>[] }
    expect(entries).toEqual([
      expect.objectContaining({ id: `question:${attId}`, kind: 'answer', session_id: session.id, tool: ATTENTION_TOOL, prompt: 'Load the PETG, please', responders: ['browser'], expires_at: expect.any(String), attention: { reason: 'blocked', on_timeout: 'proceed' } }),
      expect.objectContaining({ kind: 'approval', session_id: session.id, tool: 'mcp__stub__print', prompt: '', input_hash: expect.stringMatching(/^[0-9a-f]{64}$/), responders: ['browser', 'grant'] }),
      expect.objectContaining({ id: `question:${qId}`, kind: 'answer', session_id: s2.id, tool: 'AskUserQuestion', prompt: 'Which colour?', expires_at: null }),
    ])
    expect(entries[1]).not.toHaveProperty('attention')
    // Never the outward call's raw input.
    expect(JSON.stringify(entries)).not.toContain('"job"')

    await m.questions.answer(browser, answer(session.id, attId, ['Done, loaded']))
    const approvalId = (await m.approvals.list(browser, { pending: true }))[0]!.id
    await m.approvals.decide(browser, approvalId, false)
    await turn!.done
    await m2.interrupt(s2.id, browser)
    await t2!.done
    const after = (await (await app.request('/api/v1/ai/pending-input', { headers: UI_READ })).json()) as { entries: unknown[] }
    expect(after.entries).toEqual([])
  })

  // #815 §2: the session's tab coming back resolves its tab_disconnected request.
  it("reconnected() resolves only the session's tab_disconnected requests, as the system, never as an answer", async () => {
    const both = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const ask = (toolUseId: string, s: AttentionSpec) =>
          run.questionGate!({ tool: ATTENTION_TOOL, questions: [attentionCard(input())], toolUseId, signal: new AbortController().signal, attention: s })
        const tab = ask('toolu_tab', spec())
        const blocked = ask('toolu_blk', spec({ reason: 'blocked', timeoutS: 0.5 }))
        verdicts.push(await tab, await blocked)
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const audit = new AuditLog({ sql: db.sql })
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: both, approvalPollMs: 20, audit })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(2)
    expect(await m.questions.reconnected(session.id)).toBe(1)
    expect(await m.questions.reconnected(session.id)).toBe(0)
    await turn!.done
    expect(verdicts).toEqual([
      { answered: false, reconnected: true, message: 'the ScadBuddy tab is connected again' },
      { answered: false, timedOut: true, message: expect.any(String) },
    ])
    const rows = await db.sql`SELECT tool_use_id, outcome, answers FROM ai_questions WHERE session_id = ${session.id} ORDER BY tool_use_id`
    expect(rows).toEqual([
      { tool_use_id: 'toolu_blk', outcome: 'timed_out', answers: null },
      { tool_use_id: 'toolu_tab', outcome: 'reconnected', answers: null },
    ])
    const log = await events(m, session.id)
    expect(log.find((e) => e.type === 'question.resolved' && e.reconnected)).toMatchObject({ answered: false, reconnected: true })
    await expectPanelAccepts(log)
    expect(await db.sql`SELECT action, principal_kind, outcome FROM ai_audit WHERE kind = 'question' AND action = 'reconnected'`).toEqual([
      { action: 'reconnected', principal_kind: 'system', outcome: 'ok' },
    ])
    // Only a tab_disconnected attention request can end this way, by the schema itself.
    await expect(db.sql`UPDATE ai_questions SET outcome = 'reconnected' WHERE tool_use_id = 'toolu_blk'`).rejects.toThrow(/ai_questions_outcome_check/)
  })

  // #815 §2: reloading the tab and clicking "I'm back" race; whichever loses, the user sees no error.
  it("an \"I'm back\" answer that loses the race to reconnected() succeeds as a no-op, before or after its check; other replies do not", async () => {
    let racing: (() => Promise<unknown>) | undefined
    // reconnected() lands between answer()'s check and its update: the check saw the row open.
    const sql = new Proxy(db.sql, {
      apply(target, thisArg, args: unknown[]) {
        const query = Reflect.apply(target, thisArg, args) as Promise<unknown>
        const text = Array.isArray(args[0]) ? (args[0] as string[]).join('') : ''
        const race = racing
        if (!race || !text.includes('SELECT questions, outcome FROM ai_questions')) return query
        racing = undefined
        return query.then(async (rows) => {
          await race()
          return rows
        })
      },
    })
    const m = manager({ sql, paths: await tempPaths(), run: raising({ spec: spec() }, { spec: spec() }, { spec: spec() }), approvalPollMs: 20 })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    const next = async (...seen: string[]) => {
      let id: string | undefined
      await expect.poll(async () => {
        const [row] = await db.sql<{ id: string }[]>`
          SELECT id FROM ai_questions WHERE session_id = ${session.id} AND outcome IS NULL AND NOT (id = ANY(${seen}::uuid[]))`
        id = row?.id
        return id
      }).toBeDefined()
      return id!
    }

    const first = await pending(session.id)
    racing = () => m.questions.reconnected(session.id)
    await expect(m.questions.answer(browser, answer(session.id, first, ["I'm back"]))).resolves.toBeUndefined()
    expect(racing).toBeUndefined()

    // reconnected() won outright: the check itself sees the row resolved.
    const second = await next(first)
    expect(await m.questions.reconnected(session.id)).toBe(1)
    await expect(m.questions.answer(browser, answer(session.id, second, ["I'm back"]))).resolves.toBeUndefined()

    // Typed words that lose the race would be dropped unread: the user is told, not quietly ignored.
    const third = await next(first, second)
    racing = () => m.questions.reconnected(session.id)
    await expect(m.questions.answer(browser, answer(session.id, third, ["never mind, don't print it"]))).rejects.toMatchObject({ code: 'conflict' })
    await expect(m.questions.answer(browser, answer(session.id, third, ['Carry on without me']))).rejects.toMatchObject({ code: 'conflict' })

    await turn!.done
    expect(verdicts).toEqual([
      { answered: false, reconnected: true, message: expect.any(String) },
      { answered: false, reconnected: true, message: expect.any(String) },
      { answered: false, reconnected: true, message: expect.any(String) },
    ])
    expect(await db.sql`SELECT outcome, answers FROM ai_questions WHERE session_id = ${session.id}`).toEqual([
      { outcome: 'reconnected', answers: null },
      { outcome: 'reconnected', answers: null },
      { outcome: 'reconnected', answers: null },
    ])
    expect((await events(m, session.id)).filter((e) => e.type === 'question.resolved' && e.answered)).toEqual([])
    // Any other resolved row is still a conflict.
    await db.sql`UPDATE ai_questions SET outcome = 'cancelled' WHERE id = ${first}`
    await expect(m.questions.answer(browser, answer(session.id, first, ['late']))).rejects.toMatchObject({ code: 'conflict' })
  })

  it("a turn's browser_* calls that find no tab wait on one tab_disconnected request, and both go on when the tab is back", async () => {
    let wait: WaitForTab | undefined
    const results: TabWait[] = []
    const calls = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const signal = new AbortController().signal
        const gone = () => Promise.resolve(false)
        const a = wait!({ tool: 'browser_snapshot', toolUseId: 'toolu_s', signal, isBack: gone })
        const b = wait!({ tool: 'browser_select_plate', toolUseId: 'toolu_p', signal, isBack: gone })
        results.push(...(await Promise.all([a, b])))
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: calls,
      approvalPollMs: 20,
      mcpServers: (_session, _turn, extras) => {
        wait = extras?.waitForTab
        return {}
      },
    })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'select the plate' })
    const id = await pending(session.id)
    const [row] = await db.sql`SELECT kind, attention_reason, tool, tool_use_id FROM ai_questions WHERE id = ${id}`
    expect(row).toEqual({ kind: 'attention', attention_reason: 'tab_disconnected', tool: 'mcp__scadbuddy__browser_snapshot', tool_use_id: 'toolu_s' })
    expect(await m.questions.listPending()).toHaveLength(1)
    await m.questions.reconnected(session.id)
    await turn!.done
    expect(results).toEqual([{ back: true, why: 'reconnected' }, { back: true, why: 'reconnected' }])
  })

  // The tab came back after the call failed but before the request was recorded: nothing was waiting then.
  it('a tab already back when the wait is recorded ends it at once, not after five minutes', async () => {
    let wait: WaitForTab | undefined
    const results: TabWait[] = []
    const calls = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        results.push(await wait!({ tool: 'browser_snapshot', toolUseId: 'toolu_s', signal: new AbortController().signal, isBack: () => Promise.resolve(true) }))
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: calls,
      approvalPollMs: 20,
      mcpServers: (_session, _turn, extras) => {
        wait = extras?.waitForTab
        return {}
      },
    })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await turn!.done
    expect(results).toEqual([{ back: true, why: 'reconnected' }])
    expect(await db.sql`SELECT outcome FROM ai_questions WHERE session_id = ${session.id}`).toEqual([{ outcome: 'reconnected' }])
  })

  it("a tab wait neither counts against the model's rate limit nor replaces the model's own tab_disconnected request", async () => {
    let wait: WaitForTab | undefined
    const results: unknown[] = []
    const both = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const own = run.questionGate!({ tool: ATTENTION_TOOL, questions: [attentionCard(input())], toolUseId: 'toolu_own', signal: new AbortController().signal, attention: spec() })
        await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length, { timeout: 10_000 }).toBe(1)
        const auto = wait!({ tool: 'browser_snapshot', toolUseId: 'toolu_s', signal: new AbortController().signal, isBack: () => Promise.resolve(false) })
        // No poll for both rows pending here: the test body waits for them and then resolves
        // both, so a poll here could only lose that race and wait forever (#1472).
        results.push(...(await Promise.all([own, auto])))
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: both,
      approvalPollMs: 20,
      mcpServers: (_session, _turn, extras) => {
        wait = extras?.waitForTab
        return {}
      },
    })
    const { session: other } = await m.start(browser, { origin: 'chat', title: 'earlier' })
    // The model's limit is all but used up: its own request is the last it may make.
    for (let i = 0; i < ATTENTION_RATE_LIMIT - 1; i++) {
      await db.sql`
        INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, on_timeout,
                                  expires_at, outcome, reason, resolved_at)
        VALUES (gen_random_uuid(), ${other.id}, gen_random_uuid(), ${ATTENTION_TOOL}, ${`toolu_old${i}`}, '[]',
                'attention', 'done', 'proceed', now(), 'timed_out', 'old', now())`
    }
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE session_id = ${session.id} AND outcome IS NULL`).length).toBe(2)
    expect(await m.questions.reconnected(session.id)).toBe(2)
    await turn!.done
    expect(results).toEqual([{ answered: false, reconnected: true, message: expect.any(String) }, { back: true, why: 'reconnected' }])
  })

  /** A turn that runs `body` with its waitForTab, then ends; `m` is its manager. */
  async function withTabWait(body: (wait: WaitForTab) => Promise<void>) {
    let wait: WaitForTab | undefined
    const run = (r: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        await body(wait!)
        yield { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01, session_id: r.sessionId ?? r.resume } as unknown as SDKMessage
      })()
    const m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run,
      approvalPollMs: 20,
      mcpServers: (_session, _turn, extras) => {
        wait = extras?.waitForTab
        return {}
      },
    })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    return { m, session, turn: turn! }
  }

  it('when the last call sharing a wait stops, its card is withdrawn, not left pending on the badge', async () => {
    const results: TabWait[] = []
    const call = new AbortController()
    const { m, session, turn } = await withTabWait(async (wait) => {
      const pendingCall = wait({ tool: 'browser_snapshot', toolUseId: 'toolu_w', signal: call.signal, isBack: () => Promise.resolve(false) })
      await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(1)
      call.abort()
      results.push(await pendingCall)
      await expect.poll(async () => (await db.sql`SELECT outcome FROM ai_questions`)[0]?.outcome).toBe('cancelled')
    })
    await turn.done
    expect(results).toEqual([{ back: false, message: expect.stringMatching(/stopped while it waited/) }])
    expect(await m.questions.listPending()).toEqual([])
    const [row] = await db.sql`SELECT reason FROM ai_questions WHERE session_id = ${session.id}`
    expect(row).toEqual({ reason: 'the call was withdrawn' })
  })

  it('a failed check for an early reconnect leaves the wait waiting, and the tab coming back still ends it', async () => {
    const results: TabWait[] = []
    const { m, session, turn } = await withTabWait(async (wait) => {
      results.push(await wait({ tool: 'browser_snapshot', toolUseId: 'toolu_e', signal: new AbortController().signal, isBack: () => Promise.reject(new Error('hub gone')) }))
    })
    await pending(session.id)
    expect(await m.questions.reconnected(session.id)).toBe(1)
    await turn.done
    expect(results).toEqual([{ back: true, why: 'reconnected' }])
  })

  it('a session another principal owns gets no wait: its browser_* calls fail at once', async () => {
    let extrasSeen: unknown = 'unset'
    const m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: raising(),
      mcpServers: (_session, _turn, extras) => {
        extrasSeen = extras
        return {}
      },
    })
    const { turn } = await m.start(agentA, { origin: 'mcp', prompt: 'go' })
    await turn!.done
    expect(extrasSeen).toEqual({})
  })
})

describe('waitForTab: what each way the wait ends means for the call (#815)', () => {
  const gateOf = (verdict: QuestionVerdict) => {
    const asked: QuestionRequest[] = []
    const gate: QuestionGate = (request) => {
      asked.push(request)
      return Promise.resolve(verdict)
    }
    return { asked, gate }
  }
  const never = new AbortController().signal
  const noop = () => Promise.resolve()
  const gone = () => Promise.resolve(false)
  const call = (gate: QuestionGate) =>
    waitForTab(gate, never, noop)({ tool: 'browser_snapshot', toolUseId: 'toolu_1', signal: never, isBack: gone })

  it('parks a tab_disconnected request whose timer, at five minutes, proceeds', async () => {
    const { asked, gate } = gateOf({ answered: false, reconnected: true, message: 'x' })
    expect(await call(gate)).toEqual({ back: true, why: 'reconnected' })
    expect(asked[0]).toMatchObject({
      tool: 'mcp__scadbuddy__browser_snapshot',
      toolUseId: 'toolu_1',
      attention: { reason: 'tab_disconnected', onTimeout: 'proceed', timeoutS: TAB_WAIT_S },
      questions: [{ header: 'Tab disconnected', options: [{ label: "I'm back" }, { label: 'Carry on without the tab' }] }],
    })
  })

  it('tries again on any reply but "carry on", and never on a timeout', async () => {
    const answered = (text: string) =>
      waitForTab((request) => Promise.resolve({ answered: true, answers: { [request.questions[0]!.question]: text } }), never, noop)({
        tool: 'browser_snapshot',
        toolUseId: undefined,
        signal: never,
        isBack: gone,
      })
    expect(await answered("I'm back")).toEqual({ back: true, why: 'user_back' })
    // The user's own words are not "I'm back": the call is not run, and the model reads them.
    expect(await answered("never mind, don't print it")).toEqual({
      back: false,
      message: 'The user replied "never mind, don\'t print it" instead; the call was not run. Act on their reply.',
    })
    expect(await answered('Carry on without the tab')).toEqual({ back: false, message: expect.stringMatching(/carry on without the tab/) })
    expect(await call(gateOf({ answered: false, timedOut: true, message: 'x' }).gate)).toEqual({ back: false, message: timedOutText(TAB_WAIT_S) })
    expect(await call(gateOf({ answered: false, message: 'The user did not answer: the turn stopped first.' }).gate)).toEqual({
      back: false,
      message: 'The user did not answer: the turn stopped first.',
    })
  })

  it('after "carry on" or a timeout, the rest of the turn does not ask again: later calls fail at once', async () => {
    for (const verdict of [
      (q: string): QuestionVerdict => ({ answered: true, answers: { [q]: 'Carry on without the tab' } }),
      (): QuestionVerdict => ({ answered: false, timedOut: true, message: 'x' }),
    ]) {
      let asked = 0
      const wait = waitForTab((request) => {
        asked += 1
        return Promise.resolve(verdict(request.questions[0]!.question))
      }, never, noop)
      const first = await wait({ tool: 'browser_snapshot', toolUseId: 't1', signal: never, isBack: gone })
      const later = await wait({ tool: 'browser_click', toolUseId: 't2', signal: never, isBack: gone })
      expect(first.back).toBe(false)
      expect(later).toEqual(first)
      expect(asked).toBe(1)
    }
  })

  it('a call that comes after every waiter withdrew opens a wait of its own, not the withdrawn one', async () => {
    const requests: QuestionRequest[] = []
    const gate: QuestionGate = (request) => {
      requests.push(request)
      // As the real gate does: a withdrawn request ends as not answered.
      return new Promise((resolve) => {
        if (requests.length === 1) {
          request.signal.addEventListener('abort', () => resolve({ answered: false, message: 'The user did not answer: the turn stopped first.' }), { once: true })
        } else {
          resolve({ answered: false, reconnected: true, message: 'x' })
        }
      })
    }
    const wait = waitForTab(gate, never, noop)
    const first = new AbortController()
    const a = wait({ tool: 'browser_snapshot', toolUseId: 'toolu_a', signal: first.signal, isBack: gone })
    first.abort()
    // At once, before the withdrawn wait has settled.
    const b = wait({ tool: 'browser_click', toolUseId: 'toolu_b', signal: never, isBack: gone })
    expect(await a).toEqual({ back: false, message: expect.stringMatching(/stopped while it waited/) })
    expect(await b).toEqual({ back: true, why: 'reconnected' })
    expect(requests.map((r) => r.toolUseId)).toEqual(['toolu_a', 'toolu_b'])
    expect(requests[0]!.signal.aborted).toBe(true)
  })

  it(`opens at most ${TAB_WAITS_PER_TURN} tab waits a turn: a tab back on another replica cannot keep it waiting`, async () => {
    let asked = 0
    const wait = waitForTab(() => {
      asked += 1
      return Promise.resolve({ answered: false, reconnected: true, message: 'x' })
    }, never, noop)
    const results: TabWait[] = []
    for (let i = 0; i < TAB_WAITS_PER_TURN + 2; i++) {
      results.push(await wait({ tool: 'browser_snapshot', toolUseId: `t${i}`, signal: never, isBack: gone }))
    }
    expect(asked).toBe(TAB_WAITS_PER_TURN)
    expect(results.slice(0, TAB_WAITS_PER_TURN)).toEqual(Array(TAB_WAITS_PER_TURN).fill({ back: true, why: 'reconnected' }))
    expect(results.slice(TAB_WAITS_PER_TURN)).toEqual([
      { back: false, message: expect.stringMatching(/waited for 3 times this turn.*another agent replica/s) },
      { back: false, message: expect.stringMatching(/waited for 3 times this turn/) },
    ])
  })

  it('a call already stopped opens no wait: no card, and none of the turn\'s waits used', async () => {
    let asked = 0
    const wait = waitForTab(() => {
      asked += 1
      return Promise.resolve({ answered: false, reconnected: true, message: 'x' })
    }, never, noop)
    const stopped = new AbortController()
    stopped.abort()
    for (let i = 0; i < TAB_WAITS_PER_TURN; i++) {
      expect(await wait({ tool: 'browser_snapshot', toolUseId: `s${i}`, signal: stopped.signal, isBack: gone })).toEqual({
        back: false,
        message: 'The call stopped before it waited for the tab.',
      })
    }
    expect(asked).toBe(0)
    // The turn's waits are all still there.
    expect(await wait({ tool: 'browser_snapshot', toolUseId: 'live', signal: never, isBack: gone })).toEqual({ back: true, why: 'reconnected' })
    expect(asked).toBe(1)
  })

  it('a call that stops waiting leaves the others sharing the wait still waiting', async () => {
    let release: ((v: QuestionVerdict) => void) | undefined
    const gate: QuestionGate = () => new Promise((resolve) => (release = resolve))
    const wait = waitForTab(gate, never, noop)
    const first = new AbortController()
    const a = wait({ tool: 'browser_snapshot', toolUseId: 'toolu_a', signal: first.signal, isBack: gone })
    const b = wait({ tool: 'browser_click', toolUseId: 'toolu_b', signal: never, isBack: gone })
    first.abort()
    expect(await a).toEqual({ back: false, message: expect.stringMatching(/stopped while it waited/) })
    release!({ answered: false, reconnected: true, message: 'x' })
    expect(await b).toEqual({ back: true, why: 'reconnected' })
  })
})
