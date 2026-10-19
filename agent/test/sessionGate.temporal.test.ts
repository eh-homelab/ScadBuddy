import { WorkflowUpdateFailedError, type WorkflowHandle } from '@temporalio/client'
import { ApplicationFailure } from '@temporalio/common'
import { TestWorkflowEnvironment } from '@temporalio/testing'
import { bundleWorkflowCode, Worker, type WorkflowBundle } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import {
  buildEntry,
  type BuildEntryArgs,
  MalformedAnswerInputError,
  PROMPT_MAX,
  PromptTooLargeError,
} from '../src/gate/entry.js'
import { CANCEL_INPUT_UPDATE, PENDING_INPUT_QUERY, RESPOND_UPDATE } from '../src/gate/names.js'
import type { DenyInput, GrantInput, OpenInput, ResolveInput } from '../src/gate/store.js'
import { validateRespond } from '../src/gate/validate.js'
import type { InputEntry, Owner } from '../src/sessions/protocol.js'
import { type GateSettings, NOBODY_ANSWERED } from '../src/temporal/session/gate.js'
import { FLOW_GRANT_CHECK_ACTIVITY, SESSION_GATE_ACTIVITIES as GATE } from '../src/temporal/session/names.js'
import {
  callSignal,
  cancelAllUpdate,
  finishSignal,
  gateHarness,
  type HarnessInput,
  type HarnessState,
  loadSettingsUpdate,
  ridQuery,
  settleUpdate,
  stateQuery,
} from './support/sessionGateWorkflow.js'
import { TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// The workflow half of the tool-call gate (SessionGate, src/temporal/session/gate.ts),
// run in a test workflow (support/sessionGateWorkflow.ts) on Temporal's time-skipping
// test server, with the gate's activities faked. Each test is named after the
// agent-durable test it ports (tests/test_session_workflow.py, test_session_flow_grant.py,
// test_session_escalation.py, test_gate_entry.py); the Python ones run the whole session
// against Postgres, these the gate alone against the fake, so a row's content is checked
// as the arguments the gate sent. What was not ported, and why, is in the task report.

const WORKFLOWS = fileURLToPath(new URL('./support/sessionGateWorkflow.ts', import.meta.url))
const ME: Owner = { kind: 'browser', id: 'browser', label: 'Browser' }
const HASH = 'a'.repeat(64)
const RUN = '7e2a1c3b-4d5e-4f60-8a9b-0c1d2e3f4a5b'
const PARENT = '5e551011-0000-4000-8000-000000000001'
const PRINCIPAL = 'token:0f0e0d0c-0b0a-4908-8706-050403020100'
const PRINT = { id: 'toolu_1', name: 'print_output', input: { output_id: 'o1' } }

const ask = (question: string) => ({
  questions: [
    {
      question,
      header: 'Colour',
      multiSelect: false,
      options: [
        { label: 'Red', description: '' },
        { label: 'Blue', description: '' },
      ],
    },
  ],
})

/** What the gate's activities do in a test: they record what they were sent. */
class Fake {
  opened: OpenInput[] = []
  resolved: ResolveInput[] = []
  granted: GrantInput[] = []
  denied: DenyInput[] = []
  described: unknown[] = []
  checks: Record<string, unknown>[] = []
  tiered: unknown[] = []
  hash: string | null = HASH
  describeFails = false
  verdict: Record<string, unknown> = { verdict: 'outside', reason: 'no rule admits it', timeout_s: 0 }
  grantHolds = true
  tier: 'outward' | 'read' | 'fail' = 'outward'
  settings: GateSettings = { approvalExpiryS: 600, questionExpiryS: 3600 }
  /** Held until released: the open or the resolution is in flight meanwhile. */
  openHeld: Promise<void> | undefined
  resolveHeld: Promise<void> | undefined
  /** resolve_input fails this many times first, as a database outage does. */
  resolveFailures = 0
  resolveAttempts = 0

  activities = {
    [GATE.settings]: async () => this.settings,
    [GATE.describe]: async (args: unknown) => {
      this.described.push(args)
      if (this.describeFails) throw ApplicationFailure.nonRetryable('no key', 'ToolError')
      return { summary: '{"output_id":"o1"}', input_hash: this.hash }
    },
    [GATE.turnToolTier]: async (args: unknown) => {
      this.tiered.push(args)
      if (this.tier === 'fail') throw ApplicationFailure.nonRetryable('plugin gone', 'ToolError')
      return { tier: this.tier }
    },
    [FLOW_GRANT_CHECK_ACTIVITY]: async (args: Record<string, unknown>) => {
      this.checks.push(args)
      return this.verdict
    },
    [GATE.open]: async (args: OpenInput) => {
      this.opened.push(args)
      await this.openHeld
      return true
    },
    [GATE.grant]: async (args: GrantInput) => {
      this.granted.push(args)
      return this.grantHolds
    },
    [GATE.deny]: async (args: DenyInput) => {
      this.denied.push(args)
      return true
    },
    [GATE.resolve]: async (args: ResolveInput) => {
      this.resolveAttempts++
      if (this.resolveFailures > 0) {
        this.resolveFailures--
        throw new Error('connection refused')
      }
      await this.resolveHeld
      this.resolved.push(args)
      return true
    },
  }
}

function held(): { promise: Promise<void>; release: () => void } {
  let release!: () => void
  const promise = new Promise<void>((resolve) => {
    release = resolve
  })
  return { promise, release }
}

const answerTools = ['ask_user', 'wait_for_user']

describe.skipIf(!TEMPORAL_CLI)(`the session gate as workflow code${TEMPORAL_SKIP}`, { timeout: 60_000 }, () => {
  let env: TestWorkflowEnvironment
  let bundle: WorkflowBundle

  beforeAll(async () => {
    env = await TestWorkflowEnvironment.createTimeSkipping()
    bundle = await bundleWorkflowCode({ workflowsPath: WORKFLOWS })
  }, 180_000)
  afterAll(async () => {
    await env?.teardown()
  })

  /** One session's gate, its workers (`projects` only with `withProjects`) and a way to stop them. */
  async function session(
    overrides: Partial<HarnessInput> = {},
    options: { fake?: Fake; withProjects?: boolean } = {},
  ) {
    const fake = options.fake ?? new Fake()
    const queue = `gate-${randomUUID()}`
    const make = (taskQueue: string, workflows: boolean) =>
      Worker.create({
        connection: env.nativeConnection,
        taskQueue,
        activities: fake.activities,
        ...(workflows ? { workflowBundle: bundle } : {}),
      })
    const workers = [await make(queue, true), await make('agent-tools', false)]
    if (options.withProjects ?? true) workers.push(await make('projects', false))
    const running = workers.map((w) => w.run())
    const sessionId = randomUUID()
    const handle: WorkflowHandle<typeof gateHarness> = await env.client.workflow.start(gateHarness, {
      taskQueue: queue,
      workflowId: `gate-${sessionId}`,
      args: [{ sessionId, owner: ME, creator: ME, answerTools, taskQueue: queue, ...overrides }],
    })
    const state = (): Promise<HarnessState> => handle.query(stateQuery)
    const pending = (): Promise<(InputEntry & { expiring?: true })[]> => handle.query(PENDING_INPUT_QUERY)
    return {
      fake,
      sessionId,
      handle,
      state,
      pending,
      call: (call: { id: string; name: string; input: unknown } = PRINT) => handle.signal(callSignal, call),
      rid: (callId = PRINT.id) => handle.query(ridQuery, callId),
      parked: async (count = 1) => {
        await expect.poll(async () => (await pending()).length, { timeout: 15_000 }).toBe(count)
        return pending()
      },
      decided: async (count = 1) => {
        await expect.poll(async () => (await state()).decisions.length, { timeout: 15_000 }).toBe(count)
        return state()
      },
      respond: (args: Record<string, unknown>) => handle.executeUpdate(RESPOND_UPDATE, { args: [args] }),
      stop: async () => {
        // Ended before its workers go: an open workflow with a timer or a task nobody polls would
        // hold the test server's clock back for the tests after it.
        await handle.signal(finishSignal)
        await handle.result()
        workers.forEach((w) => w.shutdown())
        await Promise.all(running)
      },
    }
  }
  type Session = Awaited<ReturnType<typeof session>>

  async function using<T>(s: Session, body: (s: Session) => Promise<T>): Promise<T> {
    try {
      return await body(s)
    } finally {
      await s.stop()
    }
  }

  const decide = (rid: string, decision: 'approve' | 'deny', who: Owner = ME) => ({
    request_id: rid,
    response: { kind: 'approval', decision },
    responder: who,
    role: 'browser',
  })

  async function failureOf(promise: Promise<unknown>): Promise<string | undefined> {
    try {
      await promise
    } catch (err) {
      expect(err).toBeInstanceOf(WorkflowUpdateFailedError)
      const cause = (err as WorkflowUpdateFailedError).cause
      return cause instanceof ApplicationFailure ? (cause.type ?? undefined) : cause?.constructor.name
    }
    return undefined
  }

  // ---- test_session_workflow.py ----

  it('test_an_approved_call_runs_once_and_is_reported', async () => {
    await using(await session(), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      const rid = await s.rid()
      expect(entry).toMatchObject({ id: rid, kind: 'approval', tool: 'print_output', input_hash: HASH, session_id: s.sessionId, responders: ['browser', 'grant'], requested_by: ME })
      expect(s.fake.opened).toEqual([
        expect.objectContaining({ requestId: rid, sessionId: s.sessionId, kind: 'approval', tool: 'print_output', toolUseId: 'toolu_1', summary: '{"output_id":"o1"}', inputHash: HASH, prompt: '', responders: ['browser', 'grant'], requestedBy: ME }),
      ])
      expect(s.fake.opened[0]!.expiresAt).toBe(entry!.expires_at)
      expect(await s.respond(decide(rid, 'approve'))).toEqual({ kind: 'approval', outcome: 'approved' })
      const state = await s.decided()
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: true, approver: 'browser' }])
      expect(state.resolutions[rid]).toEqual({ kind: 'approval', outcome: 'approved' })
      expect(s.fake.resolved).toEqual([{ requestId: rid, outcome: 'approved', responder: ME, response: { decision: 'approve' }, rawResponse: { kind: 'approval', decision: 'approve' }, reason: null }])
      expect(await s.pending()).toEqual([])
      expect(await failureOf(s.respond(decide(rid, 'approve')))).toBe('GateRefused:stale')
    })
  })

  it('test_a_denied_call_never_runs', async () => {
    await using(await session(), async (s) => {
      await s.call()
      await s.parked()
      const rid = await s.rid()
      expect(await s.respond(decide(rid, 'deny'))).toEqual({ kind: 'approval', outcome: 'denied' })
      const state = await s.decided()
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: false, approver: 'browser' }])
      expect(state.refusals.toolu_1).toBe(`The user denied print_output (approval ${rid}); it was not run. Do not retry it unless the user asks you to.`)
      expect(state.resolutions[rid]).toEqual({ kind: 'approval', outcome: 'denied' })
    })
  })

  it('test_an_approval_expires_and_its_call_never_runs', async () => {
    await using(await session(), async (s) => {
      await s.call()
      await s.parked()
      const rid = await s.rid()
      await env.sleep('580s')
      expect((await s.state()).decisions).toEqual([])
      expect(await s.pending()).toHaveLength(1)
      await env.sleep('40s')
      const state = await s.decided()
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: false, approver: 'scadbuddy' }])
      expect(state.resolutions[rid]).toEqual({ kind: 'approval', outcome: 'expired' })
      expect(s.fake.resolved).toEqual([expect.objectContaining({ requestId: rid, outcome: 'expired', responder: { kind: 'system', id: 'scadbuddy', label: 'ScadBuddy' } })])
      expect(state.refusals.toolu_1).toContain('before it expired')
    })
  })

  it('test_a_question_is_answered', async () => {
    await using(await session(), async (s) => {
      await s.call({ id: 'toolu_q', name: 'ask_user', input: ask('Which colour?') })
      const [entry] = await s.parked()
      expect(entry).toMatchObject({ kind: 'answer', tool: 'ask_user', prompt: 'Which colour?', requested_by: null, responders: ['browser'], summary: '', input_hash: null })
      expect(s.fake.described).toEqual([])
      expect(s.fake.opened[0]!.questions).toEqual(ask('Which colour?').questions)
      const rid = await s.rid('toolu_q')
      const answer = { request_id: rid, response: { kind: 'answer', answers: { 'Which colour?': 'Red' } }, responder: ME, role: 'browser' }
      expect(await s.respond(answer)).toEqual({ kind: 'answer', outcome: 'answered' })
      const state = await s.decided()
      expect(state.decisions).toEqual([{ callId: 'toolu_q', allow: true, approver: 'browser' }])
      expect(state.resolutions[rid]).toEqual({ kind: 'answer', outcome: 'answered', response: { answers: ['Red'] } })
      expect(s.fake.resolved[0]).toMatchObject({ outcome: 'answered', response: { answers: ['Red'] } })
    })
  })

  it('test_a_question_nobody_answers_is_cancelled', async () => {
    await using(await session(), async (s) => {
      await s.call({ id: 'toolu_q', name: 'ask_user', input: ask('Which colour?') })
      await s.parked()
      const rid = await s.rid('toolu_q')
      await env.sleep('3500s')
      expect((await s.state()).decisions).toEqual([])
      await env.sleep('200s')
      const state = await s.decided()
      // An answer's call is let through to report what was recorded.
      expect(state.decisions).toEqual([{ callId: 'toolu_q', allow: true, approver: 'scadbuddy' }])
      expect(state.resolutions[rid]).toEqual({ kind: 'answer', outcome: 'cancelled' })
      expect(s.fake.resolved[0]).toMatchObject({ outcome: 'cancelled', reason: NOBODY_ANSWERED })
    })
  })

  it('test_an_attention_request_times_out_and_proceeds', async () => {
    await using(await session(), async (s) => {
      await s.call({ id: 'toolu_w', name: 'wait_for_user', input: { reason: 'blocked', message: 'Need you', timeout_s: 100, on_timeout: 'proceed' } })
      const [entry] = await s.parked()
      expect(entry!.attention).toEqual({ reason: 'blocked', on_timeout: 'proceed' })
      const rid = await s.rid('toolu_w')
      await env.sleep('80s')
      expect((await s.state()).decisions).toEqual([])
      await env.sleep('40s')
      const state = await s.decided()
      expect(state.resolutions[rid]).toEqual({ kind: 'answer', outcome: 'timed_out' })
      expect(s.fake.resolved[0]).toMatchObject({ outcome: 'timed_out' })
      expect(state.decisions).toEqual([{ callId: 'toolu_w', allow: true, approver: 'scadbuddy' }])
    })
  })

  it('test_a_malformed_question_is_refused_without_parking', async () => {
    await using(await session(), async (s) => {
      await s.call({ id: 'toolu_q', name: 'ask_user', input: { questions: 'sk-live-secret' } })
      const state = await s.decided()
      expect(state.decisions).toEqual([{ callId: 'toolu_q', allow: false, approver: 'scadbuddy' }])
      expect(s.fake.opened).toEqual([])
      expect(await s.pending()).toEqual([])
      expect(JSON.stringify(state)).not.toContain('sk-live-secret')
    })
  })

  it('test_cancel_input_resolves_the_entry_and_the_turn_goes_on', async () => {
    await using(await session(), async (s) => {
      await s.call()
      await s.parked()
      const rid = await s.rid()
      expect(await s.handle.executeUpdate(CANCEL_INPUT_UPDATE, { args: [{ reason: 'handed off' }] })).toBe('cancelled')
      const state = await s.decided()
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: false, approver: 'scadbuddy' }])
      expect(state.resolutions[rid]).toEqual({ kind: 'approval', outcome: 'cancelled' })
      expect(s.fake.resolved[0]).toMatchObject({ outcome: 'cancelled', reason: 'handed off' })
      expect(state.refusals.toolu_1).toContain('cancelled (handed off)')
      expect(await s.handle.executeUpdate(CANCEL_INPUT_UPDATE, { args: [{ reason: 'again' }] })).toBe('none')
    })
  })

  it('cuts a cancel_input reason at 200 code points, as Python slices it, never splitting a pair (#3561)', async () => {
    await using(await session(), async (s) => {
      await s.call()
      await s.parked()
      // The 200th code point is an emoji, two UTF-16 units: a unit slice would leave half of it.
      const reason = `${'a'.repeat(199)}😀 and more`
      expect(await s.handle.executeUpdate(CANCEL_INPUT_UPDATE, { args: [{ reason }] })).toBe('cancelled')
      expect(s.fake.resolved[0]?.reason).toBe(`${'a'.repeat(199)}😀`)
    })
  })

  it('test_two_responds_at_once_refuse_the_second_and_the_session_goes_on', async () => {
    const fake = new Fake()
    const gate = held()
    fake.resolveHeld = gate.promise
    await using(await session({}, { fake }), async (s) => {
      await s.call()
      await s.parked()
      const rid = await s.rid()
      const first = s.respond(decide(rid, 'approve'))
      await expect.poll(() => s.pending().then((p) => p[0]?.expiring), { timeout: 15_000 }).toBe(true)
      expect(await failureOf(s.respond(decide(rid, 'deny')))).toBe('GateRefused:resolving')
      gate.release()
      expect(await first).toEqual({ kind: 'approval', outcome: 'approved' })
      expect((await s.decided()).decisions).toEqual([{ callId: 'toolu_1', allow: true, approver: 'browser' }])
    })
  })

  it('test_a_call_that_cannot_be_described_is_refused_not_left_waiting', async () => {
    const fake = new Fake()
    fake.describeFails = true
    await using(await session({}, { fake }), async (s) => {
      await s.call()
      const state = await s.decided()
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: false, approver: 'scadbuddy' }])
      expect(fake.opened).toEqual([])
      expect(await s.pending()).toEqual([])
    })
  })

  it('test_a_respond_as_soon_as_the_row_is_written_is_taken', async () => {
    const fake = new Fake()
    const gate = held()
    fake.openHeld = gate.promise
    await using(await session({}, { fake }), async (s) => {
      await s.call()
      await expect.poll(() => fake.opened.length, { timeout: 15_000 }).toBe(1)
      const rid = await s.rid()
      // Not yet listed, as the projection lists it only once its row is written.
      expect(await s.pending()).toEqual([])
      const answered = s.respond(decide(rid, 'approve'))
      gate.release()
      expect(await answered).toEqual({ kind: 'approval', outcome: 'approved' })
      expect(fake.resolved.map((r) => r.outcome)).toEqual(['approved'])
      expect((await s.decided()).decisions).toEqual([{ callId: 'toolu_1', allow: true, approver: 'browser' }])
    })
  })

  it('test_a_stop_survives_a_database_outage_while_it_cancels_a_parked_call', async () => {
    const fake = new Fake()
    await using(await session({}, { fake }), async (s) => {
      await s.call()
      await s.parked()
      fake.resolveFailures = 3
      // Unbounded, as a Stop's cancel is: it retries with no end until the write lands.
      expect(await s.handle.executeUpdate(cancelAllUpdate, { args: [{ reason: 'the turn was interrupted' }] })).toBe(1)
      expect(fake.resolveAttempts).toBe(4)
      expect(fake.resolved.map((r) => [r.outcome, r.reason])).toEqual([['cancelled', 'the turn was interrupted']])
      expect((await s.decided()).decisions).toEqual([{ callId: 'toolu_1', allow: false, approver: 'scadbuddy' }])
    })
  })

  it('test_cancel_input_gives_up_after_30_s_and_the_entry_goes_on_waiting', async () => {
    const fake = new Fake()
    fake.resolveFailures = 1000
    await using(await session({}, { fake }), async (s) => {
      await s.call()
      await s.parked()
      const failed = failureOf(s.handle.executeUpdate(CANCEL_INPUT_UPDATE, { args: [{ reason: 'handed off' }] }))
      await env.sleep('60s')
      await failed
      // The entry is pending again: nothing was written, so nothing was decided.
      expect(await s.pending()).toHaveLength(1)
      expect((await s.state()).decisions).toEqual([])
      fake.resolveFailures = 0
      expect(await s.handle.executeUpdate(CANCEL_INPUT_UPDATE, { args: [{ reason: 'handed off' }] })).toBe('cancelled')
    })
  })

  it('test_an_expiry_in_flight_as_the_turn_ends_is_still_written', async () => {
    const fake = new Fake()
    const gate = held()
    await using(await session({}, { fake }), async (s) => {
      await s.call()
      await s.parked()
      const rid = await s.rid()
      fake.resolveHeld = gate.promise
      await env.sleep('601s')
      await expect.poll(() => s.pending().then((p) => p[0]?.expiring), { timeout: 15_000 }).toBe(true)
      // The turn ends while the expiry is being written: settle waits for it, then cancels what is left.
      const settled = s.handle.executeUpdate(settleUpdate, { args: [{ reason: 'the turn ended' }] })
      gate.release()
      await settled
      const state = await s.decided()
      expect(fake.resolved.map((r) => r.outcome)).toEqual(['expired'])
      expect(state.resolutions[rid]).toEqual({ kind: 'approval', outcome: 'expired' })
    })
  })

  it('a respond that is taken before the expiry wins it: the timer writes nothing', async () => {
    const fake = new Fake()
    const gate = held()
    fake.resolveHeld = gate.promise
    await using(await session({ settings: { approvalExpiryS: 5, questionExpiryS: 3600 } }, { fake }), async (s) => {
      await s.call()
      await s.parked()
      const rid = await s.rid()
      const answered = s.respond(decide(rid, 'approve'))
      await expect.poll(() => s.pending().then((p) => p[0]?.expiring), { timeout: 15_000 }).toBe(true)
      // The expiry passes while the respond's write is in flight.
      await new Promise((resolve) => setTimeout(resolve, 7_000))
      gate.release()
      expect(await answered).toEqual({ kind: 'approval', outcome: 'approved' })
      const state = await s.decided()
      expect(fake.resolved.map((r) => r.outcome)).toEqual(['approved'])
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: true, approver: 'browser' }])
      expect(state.resolutions[rid]).toEqual({ kind: 'approval', outcome: 'approved' })
    })
  }, 60_000)

  it('an expiry that is being written wins a respond that lands then: the respond writes nothing', async () => {
    const fake = new Fake()
    const gate = held()
    await using(await session({ settings: { approvalExpiryS: 5, questionExpiryS: 3600 } }, { fake }), async (s) => {
      await s.call()
      await s.parked()
      const rid = await s.rid()
      fake.resolveHeld = gate.promise
      await expect.poll(() => s.pending().then((p) => p[0]?.expiring), { timeout: 30_000 }).toBe(true)
      expect(await failureOf(s.respond(decide(rid, 'approve')))).toBe('GateRefused:resolving')
      gate.release()
      const state = await s.decided()
      expect(fake.resolved.map((r) => r.outcome)).toEqual(['expired'])
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: false, approver: 'scadbuddy' }])
      expect(state.resolutions[rid]).toEqual({ kind: 'approval', outcome: 'expired' })
    })
  }, 60_000)

  it('test_a_turn_tool_whose_call_is_read_runs_without_parking', async () => {
    const fake = new Fake()
    fake.tier = 'read'
    await using(await session({ turnTools: ['scadbuddy_http__http_request'] }, { fake }), async (s) => {
      const call = { id: 'toolu_h', name: 'scadbuddy_http__http_request', input: { url: 'https://example.com' } }
      await s.call(call)
      const state = await s.decided()
      expect(state.decisions).toEqual([{ callId: 'toolu_h', allow: true, approver: 'scadbuddy' }])
      expect(fake.tiered).toEqual([{ tool: call.name, input: call.input }])
      expect(fake.opened).toEqual([])
      expect(fake.described).toEqual([])
    })
  })

  it('test_an_outward_turn_tool_call_parks_and_runs_once_approved', async () => {
    await using(await session({ turnTools: ['scadbuddy_http__http_request'] }), async (s) => {
      await s.call({ id: 'toolu_h', name: 'scadbuddy_http__http_request', input: { url: 'https://example.com', method: 'POST' } })
      const [entry] = await s.parked()
      expect(entry).toMatchObject({ kind: 'approval', tool: 'scadbuddy_http__http_request' })
      await s.respond(decide(await s.rid('toolu_h'), 'approve'))
      expect((await s.decided()).decisions).toEqual([{ callId: 'toolu_h', allow: true, approver: 'browser' }])
    })
  })

  it('test_a_turn_tool_call_the_agent_cannot_tier_parks', async () => {
    const fake = new Fake()
    fake.tier = 'fail'
    await using(await session({ turnTools: ['scadbuddy_http__http_request'] }, { fake }), async (s) => {
      await s.call({ id: 'toolu_h', name: 'scadbuddy_http__http_request', input: { url: 'https://example.com' } })
      const [entry] = await s.parked()
      expect(entry).toMatchObject({ kind: 'approval' })
      expect((await s.state()).decisions).toEqual([])
    })
  })

  it('test_the_gate_settings_are_read_when_a_turn_starts', async () => {
    const fake = new Fake()
    fake.settings = { approvalExpiryS: 30, questionExpiryS: 40 }
    await using(await session({}, { fake }), async (s) => {
      expect(await s.handle.executeUpdate(loadSettingsUpdate)).toEqual({ approvalExpiryS: 30, questionExpiryS: 40 })
      await s.call()
      const [entry] = await s.parked()
      expect(Date.parse(entry!.expires_at!) - Date.parse(entry!.created_at)).toBe(30_000)
      await s.call({ id: 'toolu_q', name: 'ask_user', input: ask('Which colour?') })
      const entries = await s.parked(2)
      const question = entries.find((e) => e.kind === 'answer')!
      expect(Date.parse(question.expires_at!) - Date.parse(question.created_at)).toBe(40_000)
    })
  })

  // ---- the shared vectors, through the workflow's own respond Update ----

  describe('the pending-input vectors, through respond', () => {
    type Who = { kind: 'browser' | 'bearer'; id: string }
    type Vector = {
      name: string
      durable_only?: boolean
      entry: {
        id: string
        kind: 'approval' | 'answer'
        state: string
        input_hash: string | null
        requested_by: Who
        session_owner: Who
        session_creator: Who
        questions?: { question: string; multi_select: boolean }[]
        options?: string[]
        in_run?: boolean
      } | null
      request: { request_id: string; response: unknown; responder: { kind: string; id: string }; role: string }
      expect: { ok: true; decision?: string; answers?: string[] } | { refused: string }
    }
    const doc = JSON.parse(readFileSync(new URL('./fixtures/pending-input-vectors.json', import.meta.url), 'utf8')) as {
      vectors: Vector[]
      [key: string]: unknown
    }
    function expand(value: unknown): unknown {
      if (Array.isArray(value)) return value.map(expand)
      if (typeof value === 'object' && value !== null) {
        const v = value as Record<string, unknown>
        if (typeof v.$repeat === 'string') {
          const times = typeof v.times === 'number' ? v.times : (doc[v.times as string] as number)
          return v.$repeat.repeat(times + 1)
        }
        return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, expand(x)]))
      }
      return value
    }
    // A resolved entry is gone from the workflow (so `stale`, which the Python session
    // answers alike), and `resolving` needs a held write (test_two_responds_at_once...).
    const runnable = doc.vectors.filter((v) => v.entry && v.entry.state === 'pending' && !v.durable_only)

    it('has every pending vector to run', () => {
      expect(runnable.length).toBeGreaterThanOrEqual(40)
    })

    for (const vector of runnable) {
      it(vector.name, async () => {
        const e = vector.entry!
        const fake = new Fake()
        fake.hash = e.input_hash
        fake.verdict = { verdict: 'outside', reason: 'no rule admits it', timeout_s: 0 }
        const owner = { ...e.session_owner, label: 'o' }
        const creator = { ...e.session_creator, label: 'c' }
        const author = { ...e.requested_by, label: 'a' }
        const call = e.kind === 'approval' ? PRINT : e.options ? { id: 'toolu_w', name: 'wait_for_user', input: { reason: 'blocked', message: 'Need you', options: e.options } } : { id: 'toolu_q', name: 'ask_user', input: { questions: e.questions!.map((q) => ({ question: q.question, header: 'h', multiSelect: q.multi_select, options: [{ label: 'Red', description: '' }, { label: 'Blue', description: '' }] })) } }
        await using(await session({ owner, creator, turn: { author, ...(e.in_run ? { flow: { runId: RUN, callId: 'c1' } } : {}) } }, { fake }), async (s) => {
          await s.call(call)
          await s.parked()
          const rid = await s.rid(call.id)
          const request = { ...vector.request, request_id: vector.request.request_id === e.id ? rid : vector.request.request_id, response: expand(vector.request.response) }
          const sent = { ...request, responder: { ...request.responder, label: 'r' } }
          if ('refused' in vector.expect) {
            expect(await failureOf(s.respond(sent))).toBe(`GateRefused:${vector.expect.refused}`)
            expect(await s.pending()).toHaveLength(1)
            return
          }
          const result = (await s.respond(sent)) as { kind: string; outcome: string }
          expect(result.kind).toBe(e.kind)
          expect(result.outcome).toBe(e.kind === 'answer' ? 'answered' : vector.expect.decision === 'approve' ? 'approved' : 'denied')
          const written = fake.resolved[0]!.response as { decision?: string; answers?: string[] }
          if (vector.expect.decision) expect(written).toEqual({ decision: vector.expect.decision })
          else expect(written).toEqual({ answers: vector.expect.answers })
        })
      })
    }
  })

  // ---- test_session_flow_grant.py ----

  const flowTurn = { author: ME, flow: { runId: RUN, callId: 'c1' } }

  it('test_a_flow_turns_call_inside_the_grant_runs_with_no_entry', async () => {
    const fake = new Fake()
    fake.verdict = { verdict: 'inside', grant_id: 'g1', rule_index: 0, input_hash: HASH }
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      const state = await s.decided()
      const rid = await s.rid()
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: true, approver: 'grant:g1' }])
      expect(state.resolutions[rid]).toEqual({ kind: 'approval', outcome: 'approved' })
      expect(fake.checks).toEqual([{ run_id: RUN, request_id: rid, fn: 'tool', tool: 'print_output', args: { output_id: 'o1' }, session_id: s.sessionId, flow_call_id: 'c1' }])
      expect(rid.startsWith(`durable:${s.sessionId}:`)).toBe(true)
      expect(fake.granted).toEqual([{ requestId: rid, sessionId: s.sessionId, tool: 'print_output', summary: '{"output_id":"o1"}', inputHash: HASH, grantId: 'g1', requestedBy: ME }])
      // No entry: nothing was opened and nothing resolved.
      expect(fake.opened).toEqual([])
      expect(fake.resolved).toEqual([])
      expect(await s.pending()).toEqual([])
    })
  })

  it('test_a_turn_the_person_sends_is_no_flow_turn', async () => {
    await using(await session(), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      expect(entry!.kind).toBe('approval')
      expect(s.fake.checks).toEqual([])
    })
  })

  it('test_a_flow_turns_call_outside_the_grant_parks', async () => {
    await using(await session({ turn: flowTurn }), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      expect(entry!.kind).toBe('approval')
      expect(s.fake.checks).toHaveLength(1)
      expect((await s.state()).decisions).toEqual([])
    })
  })

  it('test_a_verdict_the_grants_record_does_not_hold_parks', async () => {
    // grant_input answers false when the recorded use is not for this call's hash and an
    // in-force grant: a verdict alone never approves.
    const fake = new Fake()
    fake.verdict = { verdict: 'inside', grant_id: 'g1', rule_index: 0, input_hash: HASH }
    fake.grantHolds = false
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      expect(entry!.kind).toBe('approval')
      expect(fake.granted).toHaveLength(1)
      expect((await s.state()).decisions).toEqual([])
      expect(fake.resolved).toEqual([])
    })
  })

  it('test_a_revoked_grant_parks_the_call', async () => {
    // The backend answers `outside` for a revoked grant, as it does for one no rule admits.
    const fake = new Fake()
    fake.verdict = { verdict: 'outside', reason: 'the grant was revoked', timeout_s: 0 }
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      expect(entry!.kind).toBe('approval')
      expect(fake.granted).toEqual([])
      expect((await s.state()).decisions).toEqual([])
    })
  })

  it('test_a_flow_turns_call_parks_when_no_projects_worker_answers', async () => {
    // Nothing serves the `projects` queue: the call parks once the check's wait runs out
    // (shortened to 2 s: the test server does not skip time while an activity is unanswered).
    await using(await session({ turn: flowTurn, grantCheckWait: '2s' }, { withProjects: false }), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      expect(entry!.kind).toBe('approval')
      expect(s.fake.checks).toEqual([])
      expect(s.fake.opened).toHaveLength(1)
    })
  }, 30_000)

  // ---- test_session_escalation.py ----

  const escalating = (extra: Record<string, unknown>): Record<string, unknown> => ({ verdict: 'outside', reason: 'print limit reached', ...extra })
  const climbing = { timeout_s: 60, parent_hop: true, hop_s: 30, parent_session: PARENT, parent_principal: PRINCIPAL }
  const SESSION: Owner = { kind: 'session' as Owner['kind'], id: PARENT, label: 'Starting session' }
  const GRANT: Owner = { kind: 'bearer', id: 'tok-c', label: 'Another agent' }
  const as = (rid: string, decision: string, role: string, who: Owner) => ({ request_id: rid, response: { kind: 'approval', decision }, responder: who, role })

  it('test_on_outside_deny_refuses_at_once_as_the_flow', async () => {
    const fake = new Fake()
    fake.verdict = escalating({ on_outside: 'deny' })
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      const state = await s.decided()
      const rid = await s.rid()
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: false, approver: `flow:${RUN}` }])
      expect(state.resolutions[rid]).toEqual({ kind: 'approval', outcome: 'denied' })
      expect(fake.denied).toEqual([{ requestId: rid, sessionId: s.sessionId, runId: RUN, tool: 'print_output', summary: '{"output_id":"o1"}', inputHash: HASH, reason: 'print limit reached', requestedBy: ME }])
      expect(state.refusals.toolu_1).toContain('The flow that sent this turn refused print_output')
      expect(state.refusals.toolu_1).toContain('print limit reached')
      expect(fake.opened).toEqual([])
      expect(await s.pending()).toEqual([])
    })
  })

  it('test_an_outside_call_waits_first_at_the_starter_session', async () => {
    const fake = new Fake()
    fake.verdict = escalating(climbing)
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      expect(fake.opened[0]).toMatchObject({
        requestId: entry!.id,
        flowRunId: RUN,
        hop: 'parent',
        parentSession: PARENT,
        parentPrincipal: PRINCIPAL,
        outsideReason: 'print limit reached',
      })
      const opened = fake.opened[0]!
      // One timer covers both hops: the parent's 30 s, then the run's 60 s.
      expect(Date.parse(opened.hopUntil!) - Date.parse(entry!.created_at)).toBe(30_000)
      expect(Date.parse(opened.expiresAt) - Date.parse(entry!.created_at)).toBe(90_000)
    })
  })

  it('test_with_no_parent_it_is_the_persons_at_once', async () => {
    const fake = new Fake()
    fake.verdict = escalating({ timeout_s: 60 })
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      const opened = fake.opened[0]!
      expect([opened.flowRunId, opened.hop, opened.parentSession ?? null, opened.hopUntil ?? null]).toEqual([RUN, 'person', null, null])
      expect(Date.parse(opened.expiresAt) - Date.parse(entry!.created_at)).toBe(60_000)
    })
  })

  it('test_the_parent_may_deny_but_never_approve_and_no_grant_decides', async () => {
    const fake = new Fake()
    fake.verdict = escalating(climbing)
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      await s.parked()
      const rid = await s.rid()
      expect(await failureOf(s.respond(as(rid, 'approve', 'parent', SESSION)))).toBe('GateRefused:forbidden')
      expect(await failureOf(s.respond(as(rid, 'approve', 'grant', GRANT)))).toBe('GateRefused:forbidden')
      expect(await s.respond(as(rid, 'deny', 'parent', SESSION))).toEqual({ kind: 'approval', outcome: 'denied' })
      const state = await s.decided()
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: false, approver: PARENT }])
      expect(fake.resolved).toEqual([{ requestId: rid, outcome: 'denied', responder: SESSION, response: { decision: 'deny' }, rawResponse: { kind: 'approval', decision: 'deny' }, reason: null }])
    })
  })

  it('test_the_timer_covers_both_hops', async () => {
    // The workflow does not know hops: at hop `parent` past `hop_until` nothing is denied
    // (the agent's sweep moves it up); only the one timer, both hops long, ends it.
    const fake = new Fake()
    fake.verdict = escalating({ timeout_s: 100, parent_hop: true, hop_s: 100, parent_session: PARENT, parent_principal: PRINCIPAL })
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      await s.parked()
      await env.sleep('150s')
      expect(await s.pending()).toHaveLength(1)
      expect((await s.state()).decisions).toEqual([])
      await env.sleep('100s')
      const state = await s.decided()
      expect(fake.resolved.map((r) => r.outcome)).toEqual(['expired'])
      expect(state.decisions).toEqual([{ callId: 'toolu_1', allow: false, approver: 'scadbuddy' }])
    })
  })

  // test_a_long_parent_hop_leaves_the_person_their_timeout (#2862): the parent's hop is
  // cut, never the person's, so the person keeps the run's whole timeout.
  it('an entry timer is never longer than a day, and a long parent hop leaves the person their timeout', async () => {
    const fake = new Fake()
    fake.verdict = escalating({ timeout_s: 3600, parent_hop: true, hop_s: 200_000, parent_session: PARENT, parent_principal: PRINCIPAL })
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      const opened = fake.opened[0]!
      const hopUntil = Date.parse(opened.hopUntil!) - Date.parse(entry!.created_at)
      const expiresAt = Date.parse(opened.expiresAt) - Date.parse(entry!.created_at)
      expect(hopUntil).toBe((86_400 - 3600) * 1000)
      expect(expiresAt).toBe(86_400_000)
      expect(expiresAt - hopUntil).toBe(3600 * 1000)
    })
  })

  // test_a_timeout_at_the_cap_still_gives_the_parent_a_one_second_hop: no room left for the
  // parent, so its hop is the 1 s floor (the sweep moves it up), and the total stays at the cap.
  it('a timeout at the cap still gives the parent a one-second hop', async () => {
    const fake = new Fake()
    fake.verdict = escalating({ timeout_s: 86_400, parent_hop: true, hop_s: 600, parent_session: PARENT, parent_principal: PRINCIPAL })
    await using(await session({ turn: flowTurn }, { fake }), async (s) => {
      await s.call()
      const [entry] = await s.parked()
      const opened = fake.opened[0]!
      expect(Date.parse(opened.hopUntil!) - Date.parse(entry!.created_at)).toBe(1000)
      expect(Date.parse(opened.expiresAt) - Date.parse(entry!.created_at)).toBe(86_400_000)
    })
  })
})

// ---- test_gate_entry.py ----

type Question = { header: string; question: string; multiSelect: unknown; options: ({ label: unknown } | { label: unknown; description: string })[] }

describe('the gate entry', () => {
  const RID = 'durable:00000000-0000-4000-8000-000000000001:run-1:toolu_1'
  const base = (over: Partial<BuildEntryArgs>): BuildEntryArgs => ({
    requestId: RID,
    kind: 'answer',
    tool: 'ask_user',
    toolInput: {},
    inputHash: null,
    requestedBy: ME,
    sessionOwner: ME,
    sessionCreator: ME,
    ...over,
  })
  const build = (tool: string, toolInput: unknown) => buildEntry(base({ tool, toolInput }))

  it('test_an_approval_carries_its_hash_and_never_its_input', () => {
    const built = buildEntry(base({ kind: 'approval', tool: 'print_output', toolInput: { api_key: 'sk-secret' }, inputHash: 'a'.repeat(64) }))
    expect(built.prompt).toBe('')
    expect(built.questions).toBeNull()
    expect(built.entry.inputHash).toBe('a'.repeat(64))
    expect(JSON.stringify(built)).not.toContain('sk-secret')
  })

  it('test_a_question_prompt_is_redacted_and_answerable', () => {
    const built = buildEntry(base({ toolInput: ask('Use key sk-secret-123?'), secrets: ['sk-secret-123'] }))
    expect(built.prompt).toBe('Use key [redacted]?')
    const valid = validateRespond(built.entry, { requestId: RID, response: { kind: 'answer', answers: { 'Use key [redacted]?': 'Red' } }, responder: ME, role: 'browser' })
    expect(valid).toEqual({ answers: ['Red'] })
  })

  it('test_an_attention_request_gets_the_default_replies', () => {
    const built = build('wait_for_user', { reason: 'blocked', message: 'Need you', on_timeout: 'proceed' })
    expect(built.entry.options).toEqual(["I'm here", 'Carry on without me'])
    expect(built.attention).toEqual({ reason: 'blocked', on_timeout: 'proceed' })
  })

  it('test_a_prompt_over_16_kib_is_refused_without_parking', () => {
    // Four questions at the 2000-character cap, in 3-byte characters: 24 KB.
    const long = { questions: [0, 1, 2, 3].map((n) => ({ ...ask('x').questions[0]!, question: `${'€'.repeat(1999)}${n}` })) }
    expect(new TextEncoder().encode(JSON.stringify(long)).length).toBeGreaterThan(PROMPT_MAX)
    expect(() => build('ask_user', long)).toThrow(PromptTooLargeError)
  })

  const attention = (extra: Record<string, unknown>) => ({ reason: 'blocked', message: 'hi', ...extra })
  it.each([
    { reason: 'sk-live-secret', message: 'hi' },
    { reason: 'done', message: 'hi' },
    attention({ on_timeout: 'stop' }),
    attention({ on_timeout: 'sk-live-secret' }),
    attention({ message: '' }),
    attention({ message: 'x'.repeat(2001) }),
    attention({ options: ['only one'] }),
    attention({ options: ['a', 'a'] }),
    attention({ options: ['a', 'b'.repeat(201)] }),
    attention({ timeout_s: 'soon' }),
    attention({ extra: 'sk-live-secret' }),
    { reason: 'blocked' },
  ])('test_a_malformed_attention_request_is_refused %#', (input) => {
    expect(() => build('wait_for_user', input)).toThrow(MalformedAnswerInputError)
  })

  const mutations: ((q: { questions: Question[]; [key: string]: unknown }) => void)[] = [
    (q) => void (q.questions = 'sk-live-secret' as never),
    (q) => void (q.questions = []),
    (q) => void (q.questions = Array(5).fill(q.questions[0])),
    (q) => void (q.questions[0]!.header = 'h'.repeat(201)),
    (q) => void (q.questions[0]!.question = ''),
    (q) => void (q.questions[0]!.options = [{ label: 'only' }]),
    (q) => void (q.questions[0]!.options[0]!.label = ''),
    (q) => void (q.questions[0]!.options[0]!.label = ['sk-live-secret']),
    (q) => void (q.questions[0]!.multiSelect = 'yes'),
    (q) => void (q.extra = 1),
  ]
  it.each(mutations.map((m, i) => [i, m] as const))('test_a_malformed_question_is_refused %i', (_i, mutate) => {
    const input = ask('Which?') as { questions: Question[]; [key: string]: unknown }
    mutate(input)
    expect(() => build('ask_user', input)).toThrow(MalformedAnswerInputError)
  })

  it('test_an_attention_card_names_its_reason_by_a_fixed_header', () => {
    const built = build('wait_for_user', { reason: 'tab_disconnected', message: 'Reopen the tab' })
    expect(built.questions![0]!.header).toBe('Tab disconnected')
    expect(built.attention).toEqual({ reason: 'tab_disconnected', on_timeout: 'proceed' })
  })
})

