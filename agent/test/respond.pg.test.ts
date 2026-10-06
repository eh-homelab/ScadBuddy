import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { attentionCard, type AttentionSpec, parseAttention } from '../src/harness/attention.js'
import { ANSWER_MAX, ATTENTION_TOOL, QUESTION_TEXT_MAX, QUESTIONS_MAX, type QuestionVerdict } from '../src/harness/questions.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { RespondBody, RESPONSE_MAX } from '../src/routes/pendingInput.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'

// POST /api/v1/ai/pending-input/{request_id} (#815; spec §6.6 `respond`): one
// route answers an approval, a question or an attention request, and refuses a
// stale id, an entry no longer pending, and a body that is not the entry's kind
// or does not fit it.

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }

const COLOUR = { question: 'Which colour?', header: 'Colour', multiSelect: false, options: [{ label: 'Red', description: '' }, { label: 'Blue', description: '' }] }
const PARTS = { question: 'Which parts?', header: 'Parts', multiSelect: true, options: [{ label: 'Lid', description: '' }, { label: 'Base', description: '' }] }

const result = { type: 'result', subtype: 'success', is_error: false, num_turns: 1, total_cost_usd: 0.01 }

const bytes = (body: string) => new TextEncoder().encode(body).length

describe('the respond cap', () => {
  it('fits the largest valid answer at its worst JSON encoding', () => {
    // QUESTIONS_MAX distinct questions of QUESTION_TEXT_MAX, each answered with ANSWER_MAX
    // control characters, which JSON.stringify writes as 6-byte \uXXXX escapes.
    const answers = Object.fromEntries(
      Array.from({ length: QUESTIONS_MAX }, (_, i) => [`${i}`.padEnd(QUESTION_TEXT_MAX, '\u0001'), '\u0001'.repeat(ANSWER_MAX)]),
    )
    const body = JSON.stringify({ kind: 'answer', answers })
    expect(RespondBody.safeParse(JSON.parse(body)).success).toBe(true)
    expect(bytes(body)).toBeLessThanOrEqual(RESPONSE_MAX)
    expect(RESPONSE_MAX).toBeGreaterThan(16 * 1024)
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`the respond route${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let verdicts: QuestionVerdict[]
  let approved: boolean[]

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    verdicts = []
    approved = []
  })
  afterEach(async () => {
    await drop()
  })

  /** A turn that parks an attention request, a two-question card and an outward call at once. */
  const parks = (run: HarnessRun): AsyncIterable<SDKMessage> =>
    (async function* () {
      await Promise.resolve()
      const signal = new AbortController().signal
      const parsed = parseAttention({ reason: 'tab_disconnected', message: 'The ScadBuddy tab closed; reopen it?' })
      if (!parsed.ok) throw new Error(parsed.error)
      const attention: AttentionSpec = { reason: 'tab_disconnected', onTimeout: 'proceed', timeoutS: 300 }
      const att = run.questionGate!({ tool: ATTENTION_TOOL, questions: [attentionCard(parsed.input)], toolUseId: 'toolu_a', signal, attention })
      const ask = run.questionGate!({ tool: 'AskUserQuestion', questions: [COLOUR, PARTS], toolUseId: 'toolu_q', signal })
      const outward = run.approvalGate!({ toolName: 'mcp__stub__print', input: { job: 'box' }, toolUseId: 'toolu_p', tier: 'outward', signal })
      // Awaited together: an interrupt rejects the approval while the questions are
      // still settling, and awaited last it would be an unhandled rejection by then.
      const [a, q, o] = await Promise.all([att, ask, outward])
      verdicts.push(a, q)
      approved.push(o.approved)
      yield { ...result, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
    })()

  /** The app over `m`, and a POST to its respond route. */
  function respondTo(m: SessionManager) {
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
    return (id: string, body: unknown, headers: Record<string, string> = UI) =>
      app.request(`/api/v1/ai/pending-input/${id}`, { method: 'POST', headers, body: typeof body === 'string' ? body : JSON.stringify(body) })
  }

  async function setUp() {
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: parks, approvalPollMs: 20 })
    const post = respondTo(m)
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'go' })
    await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_approvals WHERE decision IS NULL`).length).toBe(1)
    await expect.poll(async () => (await db.sql`SELECT 1 FROM ai_questions WHERE outcome IS NULL`).length).toBe(2)
    const ids = await ids_(m)
    return { m, session, turn: turn!, ids, post }
  }

  async function ids_(m: SessionManager) {
    const [approval] = await m.approvals.list(browser, { pending: true })
    const questions = await m.questions.listPending()
    return {
      approval: `approval:${approval!.id}`,
      inputHash: approval!.inputHash,
      attention: `question:${questions.find((q) => q.kind === 'attention')!.id}`,
      question: `question:${questions.find((q) => q.kind === 'question')!.id}`,
    }
  }

  const detail = async (r: Response) => ((await r.json()) as { detail: string }).detail

  it('answers each kind through the one route, and the parked calls get those answers', async () => {
    const { ids, post, turn } = await setUp()

    const att = await post(ids.attention, { kind: 'answer', choice: "I'm here" })
    expect(att.status).toBe(200)
    expect(await att.json()).toEqual({ id: ids.attention, kind: 'answer', outcome: 'answered' })

    const q = await post(ids.question, { kind: 'answer', answers: { 'Which parts?': ['Lid', 'Base'], 'Which colour?': 'Blue' } })
    expect(q.status).toBe(200)

    const a = await post(ids.approval, { kind: 'approval', decision: 'approve', input_hash: ids.inputHash })
    expect(a.status).toBe(200)
    expect(await a.json()).toEqual({ id: ids.approval, kind: 'approval', outcome: 'approved' })

    await turn.done
    expect(verdicts).toEqual([
      { answered: true, answers: { 'The ScadBuddy tab closed; reopen it?': "I'm here" } },
      { answered: true, answers: { 'Which colour?': 'Blue', 'Which parts?': 'Lid, Base' } },
    ])
    expect(approved).toEqual([true])

    // Nothing is pending any more: a second response to each is refused.
    expect((await post(ids.attention, { kind: 'answer', text: 'again' })).status).toBe(409)
    expect((await post(ids.question, { kind: 'answer', answers: { 'Which colour?': 'Red', 'Which parts?': 'Lid' } })).status).toBe(409)
    expect((await post(ids.approval, { kind: 'approval', decision: 'deny' })).status).toBe(409)
  })

  it("refuses a response that is not the entry's kind, or does not fit it, and leaves the entry pending", async () => {
    const { m, ids, post, session, turn } = await setUp()

    const wrongKind = await post(ids.approval, { kind: 'answer', text: 'yes' })
    expect(wrongKind.status).toBe(400)
    expect(await detail(wrongKind)).toMatch(/is an approval/)
    expect((await post(ids.question, { kind: 'approval', decision: 'approve' })).status).toBe(400)
    expect((await post(ids.attention, { kind: 'approval', decision: 'deny' })).status).toBe(400)

    // An attention request takes one of its options, or the user's own words, never both or a question's answers.
    expect(await detail(await post(ids.attention, { kind: 'answer', choice: 'Print it anyway' }))).toMatch(/must be one of/)
    expect((await post(ids.attention, { kind: 'answer', choice: "I'm here", text: 'hi' })).status).toBe(400)
    expect((await post(ids.attention, { kind: 'answer' })).status).toBe(400)
    expect((await post(ids.attention, { kind: 'answer', answers: { 'The ScadBuddy tab closed; reopen it?': 'ok' } })).status).toBe(400)

    // A question needs exactly one answer per question, keyed by its text.
    expect(await detail(await post(ids.question, { kind: 'answer', answers: { 'Which colour?': 'Red' } }))).toMatch(/exactly these questions/)
    expect((await post(ids.question, { kind: 'answer', answers: { 'Which colour?': 'Red', 'Which size?': 'M' } })).status).toBe(400)
    expect((await post(ids.question, { kind: 'answer', choice: 'Red' })).status).toBe(400)
    expect((await post(ids.question, { kind: 'answer', answers: { 'Which colour?': '', 'Which parts?': 'Lid' } })).status).toBe(400)
    // A multi-select's picks join to one answer, which ANSWER_MAX bounds as it bounds a typed one.
    const half = 'x'.repeat(ANSWER_MAX / 2)
    const joined = await post(ids.question, { kind: 'answer', answers: { 'Which colour?': 'Red', 'Which parts?': [half, half] } })
    expect(joined.status).toBe(400)
    expect(await detail(joined)).toMatch(/at most 20000 characters/)

    // The shape itself, before any entry is read.
    expect((await post(ids.approval, { kind: 'approval', decision: 'maybe' })).status).toBe(400)
    expect((await post(ids.approval, { kind: 'approval', decision: 'approve', extra: 1 })).status).toBe(400)
    expect((await post(ids.approval, 'not json')).status).toBe(400)
    expect((await post(ids.approval, { kind: 'approval', decision: 'approve', input_hash: '0'.repeat(64) })).status).toBe(409)

    expect((await m.questions.listPending()).length).toBe(2)
    expect((await m.approvals.list(browser, { pending: true })).length).toBe(1)
    await m.interrupt(session.id, browser)
    await turn.done
  })

  it('refuses a stale or unknown id, a request not from the UI, a non-JSON body and an oversized one', async () => {
    const { m, ids, post, session, turn } = await setUp()

    expect((await post('question:00000000-0000-4000-8000-000000000000', { kind: 'answer', text: 'hi' })).status).toBe(404)
    expect((await post('approval:00000000-0000-4000-8000-000000000000', { kind: 'approval', decision: 'deny' })).status).toBe(404)
    expect((await post('question:not-a-uuid', { kind: 'answer', text: 'hi' })).status).toBe(404)
    expect((await post(`durable:${session.id}:run:toolu_a`, { kind: 'answer', text: 'hi' })).status).toBe(404)
    expect((await post(ids.attention.slice('question:'.length), { kind: 'answer', text: 'hi' })).status).toBe(404)

    const { origin: _origin, ...noOrigin } = UI
    expect((await post(ids.attention, { kind: 'answer', text: 'hi' }, noOrigin)).status).toBe(403)
    expect((await post(ids.attention, { kind: 'answer', text: 'hi' }, { ...UI, 'content-type': 'text/plain' })).status).toBe(415)
    expect((await post(ids.attention, { kind: 'answer', text: 'x'.repeat(RESPONSE_MAX) })).status).toBe(413)
    // One byte over the cap is refused unread; a body of exactly the cap is read (below).
    const padded = (n: number) => `{"kind":"answer","text":"hi"}`.padEnd(n, ' ')
    expect((await post(ids.attention, padded(RESPONSE_MAX + 1))).status).toBe(413)

    expect((await m.questions.listPending()).length).toBe(2)
    expect((await post(ids.attention, padded(RESPONSE_MAX))).status).toBe(200)
    await m.interrupt(session.id, browser)
    await turn.done
  })

  // A `done` summary (#1379) outlives its turn: nothing is parked on it, and the panel's
  // Dismiss answers it through this route after the turn has ended.
  it('dismisses a done summary after its turn ended, with its Dismiss choice only', async () => {
    const done = (run: HarnessRun): AsyncIterable<SDKMessage> =>
      (async function* () {
        await Promise.resolve()
        const parsed = parseAttention({ reason: 'done', message: 'Rendered the sign.' })
        if (!parsed.ok) throw new Error(parsed.error)
        verdicts.push(
          await run.questionGate!({
            tool: ATTENTION_TOOL,
            questions: [attentionCard(parsed.input)],
            toolUseId: 'toolu_done',
            signal: new AbortController().signal,
            attention: { reason: 'done' },
          }),
        )
        yield { ...result, session_id: run.sessionId ?? run.resume } as unknown as SDKMessage
      })()
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: done, approvalPollMs: 20 })
    const post = respondTo(m)
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'render it' })
    await turn!.done
    expect(verdicts).toEqual([expect.objectContaining({ posted: true })])
    const [row] = await db.sql<{ id: string }[]>`SELECT id FROM ai_questions WHERE session_id = ${session.id} AND outcome IS NULL`
    const id = `question:${row!.id}`

    expect((await post(id, { kind: 'answer', choice: 'Print it' })).status).toBe(400)
    const dismissed = await post(id, { kind: 'answer', choice: 'Dismiss' })
    expect(dismissed.status).toBe(200)
    expect(await dismissed.json()).toEqual({ id, kind: 'answer', outcome: 'answered' })
    expect(await m.questions.listPending()).toEqual([])
    expect((await post(id, { kind: 'answer', choice: 'Dismiss' })).status).toBe(409)
  })

  it('takes answers of ANSWER_MAX, which are larger than 16 KiB as JSON (the socket path took them)', async () => {
    const { ids, post, turn } = await setUp()
    // '"' is one code unit and two bytes of JSON, so each answer is 40 000 bytes on the wire.
    const long = '"'.repeat(ANSWER_MAX)
    const body = JSON.stringify({ kind: 'answer', answers: { 'Which colour?': long, 'Which parts?': long } })
    expect(bytes(body)).toBeGreaterThan(16 * 1024)
    expect((await post(ids.question, body)).status).toBe(200)
    expect((await post(ids.attention, { kind: 'answer', text: long })).status).toBe(200)
    expect((await post(ids.approval, { kind: 'approval', decision: 'deny' })).status).toBe(200)
    await turn.done
    expect(verdicts).toEqual([
      { answered: true, answers: { 'The ScadBuddy tab closed; reopen it?': long } },
      { answered: true, answers: { 'Which colour?': long, 'Which parts?': long } },
    ])
  })
})
