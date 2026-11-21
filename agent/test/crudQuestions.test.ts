import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import type { AuditEntry } from '../src/audit/log.js'
import { CrudQuestions } from '../src/gate/crudQuestions.js'
import { QuestionError, QuestionService } from '../src/gate/questions.js'
import { ATTENTION_TOOL, type QuestionRequest } from '../src/harness/questions.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import type { ServerEvent } from '../src/sessions/protocol.js'

// The `done` summaries and the attention limits through the API's CRUD routes (#5299 step 10,
// area `questions`), against a fake API that answers as backend api/crud_agent_questions.py does
// (its tests, tests/crud/test_crud_agent_questions.py, cover the SQL, the turn fence, the cap,
// the lease and the audit rows on Postgres). The summary's text is written here with the turn's
// secrets redacted; a post names the turn's placement; the count is read only under the lease,
// and anything but a count refuses the request; ids go in the body or the query, never the path.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SID = '11111111-1111-4111-8111-111111111111'
const TURN = '22222222-2222-4222-8222-222222222222'
const QID = '33333333-3333-4333-8333-333333333333'
const ME = { kind: 'browser' as const, id: 'browser', label: 'You' }

type Seen = { method: string; path: string; body: Record<string, unknown> | undefined }
type Answer = { status?: number; body: unknown }

/** Answers each call by its route; a route with no answer is a test failure. */
class FakeApi {
  seen: Seen[] = []
  routes = new Map<string, (seen: Seen) => Answer>()

  readonly fetch: typeof fetch = (input, init) => {
    const url = new URL(String(input))
    const raw = init?.body
    const body = typeof raw === 'string' ? (JSON.parse(raw) as Record<string, unknown>) : undefined
    const seen = { method: init?.method ?? 'GET', path: url.pathname.slice(PREFIX.length) + url.search, body }
    this.seen.push(seen)
    const route = this.routes.get(`${seen.method} ${url.pathname.slice(PREFIX.length)}`)
    if (!route) throw new Error(`no route for ${seen.method} ${seen.path}`)
    const answer = route(seen)
    return Promise.resolve(
      new Response(JSON.stringify(answer.body), { status: answer.status ?? 200, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }),
    )
  }

  paths(): string[] {
    return this.seen.map((s) => `${s.method} ${s.path}`)
  }
}

/** SQL that must never be reached. */
const noSql = new Proxy(() => undefined, {
  apply: () => {
    throw new Error('ran SQL')
  },
  get: () => {
    throw new Error('ran SQL')
  },
}) as unknown as Sql

function service(options: { rateLockWaitMs?: number } = {}) {
  const api = new FakeApi()
  const committed: { sessionId: string; events: ServerEvent[]; seqs: number[] }[] = []
  const audited: AuditEntry[] = []
  const events = { committed: (sessionId: string, evs: ServerEvent[], seqs: number[]) => committed.push({ sessionId, events: evs, seqs }) } as unknown as EventLog
  const audit = { record: async (e: AuditEntry) => void audited.push(e), hash: (tool: string) => `hash-of-${tool}` }
  const questions = new QuestionService({
    sql: noSql,
    events,
    audit,
    routes: { crud: new CrudQuestions({ baseUrl: BASE, fetch: api.fetch }), use: () => true },
    ...options,
  })
  return { api, committed, audited, questions }
}

const CARD = [
  {
    question: 'Rendered the box with sk-SECRET',
    header: 'Done',
    multiSelect: false,
    options: [
      { label: 'OK', description: 'fine' },
      { label: 'More', description: 'again' },
    ],
  },
]

function doneRequest(): QuestionRequest {
  return { tool: ATTENTION_TOOL, questions: CARD, toolUseId: 'toolu_1', signal: new AbortController().signal, attention: { reason: 'done' } }
}

const context = (attempt?: string) => ({
  sessionId: SID,
  turnId: TURN,
  secrets: () => ['sk-SECRET'],
  signal: new AbortController().signal,
  turnStartedAt: '2026-11-15 10:00:00.123456+00',
  ...(attempt === undefined ? {} : { attempt }),
})

const LOGGED = [{ session_id: SID, events: [{ v: 1, type: 'question.asked' }], seqs: [7] }]

describe('the done summaries through the CRUD routes', () => {
  it("posts the turn's summary, written here, with the placement that holds the turn", async () => {
    const { api, committed, questions } = service()
    api.routes.set('POST /questions/done-facts', () => ({
      body: {
        windows: [],
        touches: [
          {
            at: '2026-11-15T10:00:01Z',
            in_turn: true,
            in_away: false,
            before_asked: false,
            tool: 'mcp__scadbuddy__create_model',
            resource_type: 'model',
            resource_id: 'box-sk-SECRET',
            action: 'created',
            model_slug: null,
          },
          // Before the turn and outside every window: the parked turn's own, not this one's.
          {
            at: '2026-11-15T09:00:00Z',
            in_turn: false,
            in_away: false,
            before_asked: true,
            tool: 'x',
            resource_type: 'model',
            resource_id: 'old',
            action: 'created',
            model_slug: null,
          },
        ],
      },
    }))
    api.routes.set('POST /questions/done', () => ({ body: { asked: true, limited: null, logged: LOGGED } }))
    const verdict = await questions.gate(context('run-1/1'))(doneRequest())
    expect(verdict).toEqual({ answered: false, posted: true, message: 'posted' })
    expect(api.seen[0]!.body).toEqual({ session_id: SID, turn_id: TURN, since: '2026-11-15 10:00:00.123456+00' })
    const post = api.seen[1]!.body!
    expect(post).toMatchObject({ session_id: SID, turn_id: TURN, attempt: 'run-1/1', tool: ATTENTION_TOOL, tool_use_id: 'toolu_1', unattended: false })
    expect(JSON.stringify(post)).not.toContain('sk-SECRET')
    expect(post.summary).toContain('created model')
    expect(post.summary).not.toContain('old')
    expect(committed).toEqual([{ sessionId: SID, events: LOGGED[0]!.events, seqs: [7] }])
  })

  it('posts nothing without the turn placement, and tells the model', async () => {
    const { api, questions } = service()
    const verdict = await questions.gate(context())(doneRequest())
    expect(verdict).toMatchObject({ answered: false, message: expect.stringContaining('not asked') })
    expect(api.seen).toEqual([])
  })

  it("relays the API's cap and a post the fence refused", async () => {
    const { api, questions } = service()
    api.routes.set('POST /questions/done-facts', () => ({ body: { windows: [], touches: [] } }))
    const answers = [
      { asked: false, limited: 'The summary was not posted: this turn already posted 3, the most a turn may.', logged: [] },
      { asked: false, limited: null, logged: [] },
    ]
    api.routes.set('POST /questions/done', () => ({ body: answers.shift() }))
    expect(await questions.gate(context('run-1/1'))(doneRequest())).toEqual({ answered: false, message: expect.stringContaining('already posted 3') })
    expect(await questions.gate(context('run-1/1'))(doneRequest())).toEqual({ answered: false, message: expect.stringContaining('not asked') })
  })
})

describe('the attention limit through the CRUD routes', () => {
  const limitContext = { sessionId: SID, turnId: TURN, toolUseId: 'toolu_1', tool: ATTENTION_TOOL, reason: 'blocked', own: true }

  it('counts under the lease, opens before it is released, and lets the request through', async () => {
    const { api, questions } = service()
    const order: string[] = []
    api.routes.set('POST /leases/attention-rate', () => (order.push('acquire'), { body: { expires_at: 'x' } }))
    api.routes.set('POST /questions/attention-count', () => (order.push('count'), { body: { n: 9 } }))
    api.routes.set('DELETE /leases/attention-rate', () => (order.push('release'), { body: {} }))
    const refused = await questions.attentionLimit(limitContext, async () => void order.push('open'))
    expect(refused).toBeUndefined()
    expect(order).toEqual(['acquire', 'count', 'open', 'release'])
    const token = (api.seen[0]!.body as { token: string }).token
    expect(api.seen[1]!.body).toEqual({ own: true, lease_token: token })
    expect(api.seen[2]!.path).toBe(`/leases/attention-rate?token=${token}`)
  })

  it('refuses at the limit, with an audit row, and never opens', async () => {
    const { api, audited, questions } = service()
    api.routes.set('POST /leases/attention-rate', () => ({ body: { expires_at: 'x' } }))
    api.routes.set('POST /questions/attention-count', () => ({ body: { n: 10 } }))
    api.routes.set('DELETE /leases/attention-rate', () => ({ body: {} }))
    let opened = false
    const refused = await questions.attentionLimit(limitContext, async () => void (opened = true))
    expect(refused).toContain('at most 10 attention requests')
    expect(opened).toBe(false)
    expect(audited.map((a) => a.action)).toEqual(['rate_limited'])
  })

  it('fails closed: a lost lease, a busy one past the wait, or an API error refuses and never opens', async () => {
    for (const setUp of [
      (api: FakeApi) => {
        api.routes.set('POST /leases/attention-rate', () => ({ body: { expires_at: 'x' } }))
        api.routes.set('POST /questions/attention-count', () => ({ status: 409, body: { detail: 'lost', reason: 'lease-lost' } }))
        api.routes.set('DELETE /leases/attention-rate', () => ({ body: {} }))
      },
      (api: FakeApi) => api.routes.set('POST /leases/attention-rate', () => ({ status: 409, body: { detail: 'busy', reason: 'busy' } })),
      (api: FakeApi) => api.routes.set('POST /leases/attention-rate', () => ({ status: 500, body: {} })),
    ]) {
      const { api, questions } = service({ rateLockWaitMs: 120 })
      setUp(api)
      let opened = false
      const refused = await questions.attentionLimit(limitContext, async () => void (opened = true))
      expect(refused).toContain('could not check the attention request limit')
      expect(opened).toBe(false)
    }
  })
})

describe('a summary dismissed through the CRUD routes', () => {
  const message = (id = QID, answers = ['ok']) => ({ v: 1 as const, type: 'question.answer' as const, sessionId: SID, id, answers })

  it("reads the row's tool for the hash, then writes once on the API", async () => {
    const { api, committed, questions } = service()
    api.routes.set('GET /questions/entry', () => ({
      body: { entry: { session_id: SID, kind: 'attention', questions: CARD, outcome: null, reason: null, tool: ATTENTION_TOOL } },
    }))
    api.routes.set('POST /questions/answer', () => ({ body: { logged: LOGGED } }))
    await questions.answer(ME, message(), { clientIp: '10.0.0.1' })
    expect(api.seen[0]!.path).toBe(`/questions/entry?id=${QID}`)
    expect(api.seen[1]!.body).toEqual({
      id: QID,
      session_id: SID,
      answers: ['ok'],
      by: ME,
      input_hash: `hash-of-${ATTENTION_TOOL}`,
      surface: 'http',
      client_ip: '10.0.0.1',
    })
    expect(committed).toHaveLength(1)
  })

  it("maps the API's refusals, and never sends a malformed id", async () => {
    const { api, questions } = service()
    api.routes.set('GET /questions/entry', () => ({
      body: { entry: { session_id: SID, kind: 'attention', questions: CARD, outcome: null, reason: null, tool: ATTENTION_TOOL } },
    }))
    api.routes.set('POST /questions/answer', () => ({ status: 409, body: { detail: 'no longer waiting', reason: 'conflict' } }))
    await expect(questions.answer(ME, message())).rejects.toMatchObject({ name: 'QuestionError', code: 'conflict' })
    const before = api.seen.length
    await expect(questions.answer(ME, message('../../areas?x=1#y'))).rejects.toBeInstanceOf(QuestionError)
    expect(api.seen).toHaveLength(before)
  })

  it("is not found when the row is another session's", async () => {
    const { api, questions } = service()
    api.routes.set('GET /questions/entry', () => ({
      body: { entry: { session_id: TURN, kind: 'attention', questions: CARD, outcome: null, reason: null, tool: ATTENTION_TOOL } },
    }))
    await expect(questions.answer(ME, message())).rejects.toMatchObject({ code: 'not_found' })
    expect(api.paths()).toEqual([`GET /questions/entry?id=${QID}`])
  })
})

describe('the reads through the CRUD routes', () => {
  it('lists the pending summaries as the SQL did, and says when there were more', async () => {
    const { api, questions } = service()
    const row = {
      id: QID,
      session_id: SID,
      kind: 'attention',
      tool: ATTENTION_TOOL,
      tool_use_id: 'toolu_1',
      questions: CARD,
      attention_reason: 'done',
      on_timeout: null,
      summary: 's',
      created_at: '2026-11-15T10:00:00.123456+00:00',
      expires_at: null,
    }
    api.routes.set('POST /questions/pending', () => ({ body: { questions: Array.from({ length: 501 }, () => row) } }))
    const listed = await questions.listPending(ME)
    expect(api.seen[0]!.body).toEqual({ kind: 'browser', id: 'browser' })
    expect(listed.summariesTruncated).toBe(true)
    expect(listed.questions).toHaveLength(500)
    expect(listed.questions[0]).toEqual({
      id: QID,
      sessionId: SID,
      kind: 'attention',
      tool: ATTENTION_TOOL,
      toolUseId: 'toolu_1',
      questions: CARD,
      attentionReason: 'done',
      onTimeout: null,
      summary: 's',
      createdAt: '2026-11-15T10:00:00.123Z',
      expiresAt: null,
    })
  })

  it('reads a row by id in the query, and a malformed one names nothing', async () => {
    const { api, questions } = service()
    expect(await questions.entry('../x')).toBeUndefined()
    expect(api.seen).toEqual([])
    api.routes.set('GET /questions/entry', () => ({ body: { entry: null } }))
    expect(await questions.entry(QID)).toBeUndefined()
  })

  it("audits an entry's answer read through the API, its request id encoded in the query", async () => {
    const { api, audited, questions } = service()
    const rid = `durable:${SID}:run-1:../../areas?x=1#y`
    api.routes.set('GET /questions/gate-answer', () => ({
      body: { answer: { session_id: SID, tool: 'wait_for_user', requested_at: '2026-11-15T10:00:00Z', response: { answers: ['a', 'b'] } } },
    }))
    await questions.auditGateAnswer(ME, rid)
    expect(api.seen[0]!.path).toBe(`/questions/gate-answer?request_id=${encodeURIComponent(rid)}`)
    expect(audited).toHaveLength(1)
    expect(audited[0]).toMatchObject({ action: 'answered', sessionId: SID, detail: `wait_for_user question ${rid}: 2 answers` })
  })
})
