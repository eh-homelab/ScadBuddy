import { describe, expect, it } from 'vitest'
import { attentionCard, attentionSpec, parseAttention } from '../src/harness/attention.js'
import { ATTENTION_TOOL, type QuestionRequest } from '../src/harness/questions.js'
import { ATTENTION_GRACE_MS, attentionGate, type AttentionGateDeps, type HeldRow, TAB_WAIT_CALL } from '../src/sessions/attentionGate.js'

// The attention gate of an AgentSession turn (#5003), on stand-ins for TurnHost's park and hold:
// each opened entry's row is answered by the test. End to end, against the workflow and the real
// Claude Code: test/sessionPark.temporal.test.ts and test/sessionRelease.temporal.test.ts.

type Opened = { call: { id: string; name: string; input: Record<string, unknown> }; holdMs: number | undefined; answer: (row: HeldRow) => void }

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

const answered = (reply: string): HeldRow => ({ outcome: 'answered', response: { answers: [reply] }, reason: null, responder: { kind: 'browser' } })
const timedOut: HeldRow = { outcome: 'timed_out', response: null, reason: null, responder: { kind: 'system' } }
const reconnected: HeldRow = { outcome: 'cancelled', response: { reconnected: true }, reason: 'the ScadBuddy tab is connected again', responder: { kind: 'system' } }

function harness(over: Partial<AttentionGateDeps> = {}) {
  const opened: Opened[] = []
  /** Every row a hold waits on, by request id: settled once the test answers it. */
  const rows = new Map<string, ReturnType<typeof deferred<HeldRow>>>()
  const rowOf = (rid: string) => {
    let row = rows.get(rid)
    if (!row) rows.set(rid, (row = deferred<HeldRow>()))
    return row
  }
  const ended: string[] = []
  const limits: { own: boolean; reason: string }[] = []
  const stop = new AbortController()
  const releasing = new AbortController()
  const deps: AttentionGateDeps = {
    session: { id: 's1' },
    turnId: 't1',
    stop: stop.signal,
    secrets: () => ['sk-secret-1234'],
    park: async (call, signal, options) => {
      const rid = `durable:s1:run:${call.id}`
      opened.push({ call: call as Opened['call'], holdMs: options.holdMs, answer: (row) => rowOf(rid).resolve(row) })
      options.opened?.(rid)
      // As TurnHost's park: its hold ends with the release too.
      return { requestId: rid, row: await hold(rid, AbortSignal.any([signal, releasing.signal])) }
    },
    // As the workflow's withdraw_input: the call's entry ends cancelled, saying why.
    withdraw: async (callId, why) => {
      rowOf(`durable:s1:run:${callId}`).resolve({ outcome: 'cancelled', response: null, reason: why, responder: { kind: 'system' } })
      return true
    },
    hold: (rid, signal) => hold(rid, signal),
    holdMs: 300_000,
    released: releasing.signal,
    release: () => ({ answered: false, message: 'released', interrupt: true }),
    endTurn: (why) => ended.push(why),
    questions: {
      attentionLimit: async ({ own, reason }, open) => {
        limits.push({ own, reason })
        await open()
        return undefined
      },
    },
    ...over,
  }
  function hold(rid: string, signal: AbortSignal): Promise<HeldRow> {
    return new Promise((resolve, reject) => {
      if (signal.aborted) return reject(new Error('aborted'))
      signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
      void rowOf(rid).promise.then(resolve)
    })
  }
  return { gate: attentionGate(deps), opened, ended, limits, stop, releasing }
}

/** A request as harness/attention.ts's handler (the model's) or sessions/manager.ts waitForTab (a browser call's) makes it. */
function request(input: Record<string, unknown>, own = true, toolUseId = own ? 'toolu_own' : 'toolu_tab') {
  const parsed = parseAttention(input)
  if (!parsed.ok) throw new Error(parsed.error)
  const spec = attentionSpec(parsed.input)
  if (spec.reason === 'done') throw new Error('not a wait')
  const req: QuestionRequest = {
    tool: own ? ATTENTION_TOOL : 'mcp__scadbuddy__browser_snapshot',
    questions: [attentionCard(parsed.input)],
    toolUseId,
    signal: new AbortController().signal,
    attention: spec,
  }
  return { req, spec, question: req.questions[0]!.question }
}

const until = async (check: () => boolean) => expect.poll(check, { timeout: 2_000, interval: 5 }).toBe(true)

describe('an AgentSession turn`s attention gate', () => {
  it('parks the model`s request as wait_for_user, redacted, and the reply is the answer', async () => {
    const h = harness()
    const { req, spec, question } = request({ reason: 'blocked', message: 'Key sk-secret-1234 failed; go on?', options: ['Go', 'Stop'] })
    const verdict = h.gate(req, spec)
    await until(() => h.opened.length === 1)
    expect(h.opened[0]!.call).toEqual({
      id: 'toolu_own',
      name: 'wait_for_user',
      input: { reason: 'blocked', message: 'Key [redacted] failed; go on?', options: ['Go', 'Stop'], timeout_s: 300, on_timeout: 'proceed' },
    })
    // Its timer is within the hold: held to it, and the grace for the workflow's write.
    expect(h.opened[0]!.holdMs).toBe(300_000 + ATTENTION_GRACE_MS)
    h.opened[0]!.answer(answered('Go'))
    expect(await verdict).toEqual({ answered: true, answers: { [question]: 'Go' } })
    expect(h.limits).toEqual([{ own: true, reason: 'blocked' }])
  })

  it("a timer longer than the hold is left to the hold, and `stop`'s timeout ends the turn", async () => {
    const h = harness()
    const { req, spec } = request({ reason: 'blocked', message: 'Need you', timeout_s: 900, on_timeout: 'stop' })
    const verdict = h.gate(req, spec)
    await until(() => h.opened.length === 1)
    expect(h.opened[0]!.holdMs).toBeUndefined()
    h.opened[0]!.answer(timedOut)
    expect(await verdict).toEqual({ answered: false, message: 'The user did not reply: nobody replied to the attention request in 900 s (on_timeout: stop). Your turn ends here.' })
    expect(h.ended).toEqual(['the turn ended: nobody replied to the attention request in 900 s (on_timeout: stop)'])
  })

  it("`proceed`'s timeout is timed_out, never an answer; the tab's return is `reconnected`", async () => {
    const h = harness()
    const a = request({ reason: 'question', message: 'Which?' })
    const first = h.gate(a.req, a.spec)
    await until(() => h.opened.length === 1)
    h.opened[0]!.answer(timedOut)
    expect(await first).toMatchObject({ answered: false, timedOut: true })
    const b = request({ reason: 'tab_disconnected', message: 'Tab?', timeout_s: 300 }, false)
    const second = h.gate(b.req, b.spec)
    await until(() => h.opened.length === 2)
    expect(h.opened[1]!.call.id).toBe(`${TAB_WAIT_CALL}toolu_tab`)
    h.opened[1]!.answer(reconnected)
    expect(await second).toEqual({ answered: false, reconnected: true, message: 'the ScadBuddy tab is connected again' })
    expect(h.ended).toEqual([])
  })

  it('a rate limit refuses the request before anything parks', async () => {
    const h = harness({ questions: { attentionLimit: async () => 'The user was not asked: too many.' } })
    const { req, spec } = request({ reason: 'blocked', message: 'Need you' })
    expect(await h.gate(req, spec)).toEqual({ answered: false, message: 'The user was not asked: too many.' })
    expect(h.opened).toEqual([])
  })

  it("#1308: a browser call's tab wait joins the model's open tab_disconnected card, and one reply ends both", async () => {
    const h = harness()
    const own = request({ reason: 'tab_disconnected', message: 'Please reopen the tab' })
    const tab = request({ reason: 'tab_disconnected', message: 'Tab?', timeout_s: 300 }, false)
    const first = h.gate(own.req, own.spec)
    await until(() => h.opened.length === 1)
    const second = h.gate(tab.req, tab.spec)
    // No second card, and no second count against a limit.
    await new Promise((r) => setTimeout(r, 20))
    expect(h.opened).toHaveLength(1)
    expect(h.limits).toHaveLength(1)
    h.opened[0]!.answer(answered("I'm here"))
    expect(await first).toEqual({ answered: true, answers: { [own.question]: "I'm here" } })
    expect(await second).toEqual({ answered: true, answers: { [tab.question]: "I'm here" } })
  })

  it('a joined card that ends without the user (its own timer) leaves the joiner to ask on its own, to its own deadline', async () => {
    const h = harness()
    const tab = request({ reason: 'tab_disconnected', message: 'Tab?', timeout_s: 300 }, false)
    const own = request({ reason: 'tab_disconnected', message: 'Please reopen the tab', timeout_s: 60 })
    const first = h.gate(tab.req, tab.spec)
    await until(() => h.opened.length === 1)
    const joiner = h.gate(own.req, own.spec)
    await new Promise((r) => setTimeout(r, 20))
    h.opened[0]!.answer(timedOut)
    expect(await first).toMatchObject({ answered: false, timedOut: true })
    await until(() => h.opened.length === 2)
    // The model's own card now, with what is left of its 60 s.
    expect(h.opened[1]!.call.id).toBe('toolu_own')
    expect(h.opened[1]!.call.input.timeout_s).toBeGreaterThan(55)
    expect(h.opened[1]!.call.input.timeout_s).toBeLessThanOrEqual(60)
    h.opened[1]!.answer(answered('Carry on without me'))
    expect(await joiner).toEqual({ answered: true, answers: { [own.question]: 'Carry on without me' } })
  })

  it('a release while a request is held ends the turn with the released answer', async () => {
    const h = harness()
    const { req, spec } = request({ reason: 'blocked', message: 'Need you' })
    const verdict = h.gate(req, spec)
    await until(() => h.opened.length === 1)
    h.releasing.abort()
    expect(await verdict).toEqual({ answered: false, message: 'released', interrupt: true })
  })
})
