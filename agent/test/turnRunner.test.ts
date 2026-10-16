import type { HookCallbackMatcher, HookEvent, HookInput } from '@anthropic-ai/claude-agent-sdk'
import { describe, expect, it, vi } from 'vitest'
import type { ApprovalRequest } from '../src/harness/permissions.js'
import { ROOT_CONTEXT } from '@opentelemetry/api'
import { createTurnSlot, slotHooks, slotHttpContext, type TurnContext } from '../src/sessions/turnSlot.js'
import { type FingerprintParts, TurnRunner } from '../src/sessions/turnRunner.js'
import { TurnTrace } from '../src/telemetry/turn.js'
import { PgHttpBodies } from '../src/sessions/httpBodies.js'
import { turnRunnerForTest, type TurnRunnerTestOptions, unusedSql } from './support/turnRunner.js'

// TurnRunner (unified runtime plan, Task 1.2; spec 2026-10-13 §5.3): every
// callback it assembles reads the current turn from the slot, so the options
// can outlive one turn.

const request = (toolUseId: string): ApprovalRequest => ({
  toolName: 'mcp__scadbuddy__send_to_bambuddy',
  input: {},
  toolUseId,
  tier: 'outward',
  signal: new AbortController().signal,
})

const turnContext = (turnId: string): TurnContext => ({
  turnId,
  principal: {},
  approvalGate: () => Promise.resolve({ approved: false, message: 'unused' }),
  trace: new TurnTrace({ sessionId: 's', turnId, parent: ROOT_CONTEXT, tierOf: () => 'read' }),
  signalInterrupt: () => {},
})

describe('TurnRunner', () => {
  it('assembles callbacks that read the current turn, and deny between turns', async () => {
    const { runner, session, principal, gateCalls } = await turnRunnerForTest()
    const turn = await runner.assemble(session, principal)
    // The assembly's own slot: what every turn its process serves sets (sessions/warmTurn.ts).
    const { slot } = turn
    const decide = turn.run.approvalGate!
    expect(await decide(request('t1'))).toMatchObject({ approved: false, message: 'no turn is running' })
    const ask = turn.run.questionGate!
    expect(await ask({ tool: 'AskUserQuestion', questions: [], toolUseId: 't0', signal: new AbortController().signal }))
      .toMatchObject({ answered: false, message: 'no turn is running' })
    slot.set(gateCalls.context('turn-1'))
    await decide(request('t1'))
    expect(gateCalls.seen).toEqual([{ turnId: 'turn-1', toolUseId: 't1' }])
    slot.clear()
    expect(await decide(request('t2'))).toMatchObject({ approved: false, message: 'no turn is running' })
    expect(gateCalls.seen).toHaveLength(1)
  })

  it("runs a Workflow agent's chat on its own model, effort and subagent, and fingerprints them", async () => {
    const { runner, session, principal } = await turnRunnerForTest()
    const plain = await runner.assemble(session, principal)
    const agent = { ...session, agentModel: 'claude-haiku-test', agentEffort: 'low' as const, agentType: 'model-author' }
    const turn = await runner.assemble(agent, principal)
    expect(turn.run).toMatchObject({ model: 'claude-haiku-test', effort: 'low', agent: { name: 'model-author' } })
    expect(turn.run.agent?.definition.prompt).toContain('You author ScadBuddy templates')
    expect(turn.fingerprintParts).toMatchObject({ model: 'claude-haiku-test', effort: 'low', agentType: 'model-author' })
    expect(turn.fingerprintParts).not.toEqual(plain.fingerprintParts)
  })

  it('fingerprints an unchanged configuration the same on every assembly, and a moved plugin URL differently', async () => {
    const { runner, session, principal } = await turnRunnerForTest()
    const first = await runner.assemble(session, principal)
    const second = await runner.assemble(session, principal)
    // Each assembly registers its own loopback route, which the fingerprint never names.
    expect(second.run.remotePlugins![0]!.url).not.toBe(first.run.remotePlugins![0]!.url)
    expect(second.fingerprintParts).toEqual(first.fingerprintParts)
    expect(first.fingerprintParts.plugins).toEqual([
      {
        id: 'hindsight',
        url: 'https://hindsight.example/mcp/bank/',
        headerVersion: expect.stringMatching(/^[\w-]{43}$/),
        toolTiers: ['recall=read'],
        disabledTools: [],
      },
    ])

    const moved = await turnRunnerForTest({ pluginUrl: 'https://hindsight.example/mcp/other/' })
    const elsewhere = await moved.runner.assemble(session, principal)
    expect(elsewhere.fingerprintParts).not.toEqual(first.fingerprintParts)
    expect(elsewhere.fingerprintParts.plugins[0]!.url).toBe('https://hindsight.example/mcp/other/')
  })

  // Each input fixed at spawn that changes what the process permits moves its
  // own field, and nothing else does (spec §5.3, "Reuse rule").
  it.each<[string, TurnRunnerTestOptions, keyof FingerprintParts]>([
    ["a plugin's tool tiers", { pluginToolTiers: { recall: 'outward' } }, 'plugins'],
    ["a plugin's disabled tools", { pluginDisabledTools: ['recall'] }, 'plugins'],
    ['a raised built-in tier', { raisedTiers: { get_model: 'outward' } }, 'raisedTiers'],
    ['the http_request setting', { httpRequest: false }, 'httpRequest'],
    ["a package's allow_refused approval", { allowRefused: true }, 'allowedPackageHashes'],
    ['the Hindsight recall query cap', { recallMaxQueryChars: 12 }, 'recallMaxQueryChars'],
  ])('fingerprints %s', async (_what, changed, field) => {
    const base = await turnRunnerForTest()
    const before = await base.runner.assemble(base.session, base.principal)
    const again = await base.runner.assemble(base.session, base.principal)
    expect(again.fingerprintParts).toEqual(before.fingerprintParts)
    const other = await turnRunnerForTest(changed)
    const after = await other.runner.assemble(base.session, base.principal)
    expect(after.fingerprintParts[field]).not.toEqual(before.fingerprintParts[field])
    const { [field]: _a, ...restAfter } = after.fingerprintParts
    const { [field]: _b, ...restBefore } = before.fingerprintParts
    expect(restAfter).toEqual(restBefore)
  })

  type Hooks = Partial<Record<HookEvent, HookCallbackMatcher[]>>
  const hookOptions = { signal: new AbortController().signal }
  const input = (event: string, extra: Record<string, unknown> = {}) =>
    ({ hook_event_name: event, session_id: 's', transcript_path: '/t', cwd: '/', ...extra }) as unknown as HookInput
  /** Hooks shaped like `shape`, each recording `<owner> <event> <matcher>.<hook>` when it runs, and answering with it. */
  function recording(shape: Hooks, owner: string, calls: string[]): Hooks {
    return Object.fromEntries(
      Object.entries(shape).map(([event, matchers]) => [
        event,
        matchers!.map((m, i) => ({
          ...m,
          hooks: m.hooks.map((_, j) => () => {
            const call = `${owner} ${event} ${i}.${j}`
            calls.push(call)
            return Promise.resolve({ systemMessage: call })
          }),
        })),
      ]),
    )
  }
  /** Fires every hook of `hooks` once, in order; their answers. */
  async function fireAll(hooks: Hooks): Promise<unknown[]> {
    const out: unknown[] = []
    for (const [event, matchers] of Object.entries(hooks)) {
      for (const m of matchers!) for (const hook of m.hooks) out.push(await hook(input(event), 'toolu_1', hookOptions))
    }
    return out
  }

  it("dispatches each slot hook to the same place in the current turn's hooks, and does nothing between turns (#3453)", async () => {
    // Two matchers of two hooks under two events: a dispatch that mixed up an index or an event shows.
    const shape: Hooks = {
      UserPromptSubmit: [{ hooks: [() => Promise.resolve({}), () => Promise.resolve({})] }, { matcher: 'x', hooks: [() => Promise.resolve({}), () => Promise.resolve({})] }],
      Stop: [{ hooks: [() => Promise.resolve({})] }, { hooks: [() => Promise.resolve({}), () => Promise.resolve({})] }],
    }
    const slot = createTurnSlot()
    const calls: string[] = []
    const memoryOf = (turnId: string) => ({ hooks: recording(shape, turnId, calls), settled: () => Promise.resolve() })
    const hooks = slotHooks(shape, slot, (turn) => turn.memory?.hooks)
    expect(hooks.UserPromptSubmit?.[1]?.matcher).toBe('x')
    const every = ['UserPromptSubmit 0.0', 'UserPromptSubmit 0.1', 'UserPromptSubmit 1.0', 'UserPromptSubmit 1.1', 'Stop 0.0', 'Stop 1.0', 'Stop 1.1']

    expect(await fireAll(hooks)).toEqual(Array(every.length).fill({}))
    expect(calls).toEqual([])
    slot.set({ ...turnContext('turn-1'), memory: memoryOf('turn-1') })
    expect(await fireAll(hooks)).toEqual(every.map((at) => ({ systemMessage: `turn-1 ${at}` })))
    slot.set({ ...turnContext('turn-2'), memory: memoryOf('turn-2') })
    await fireAll(hooks)
    expect(calls).toEqual([...every.map((at) => `turn-1 ${at}`), ...every.map((at) => `turn-2 ${at}`)])
    // A turn without memory hooks, and no turn: nothing runs.
    slot.set(turnContext('turn-3'))
    expect(await fireAll(hooks)).toEqual(Array(every.length).fill({}))
    slot.clear()
    expect(await fireAll(hooks)).toEqual(Array(every.length).fill({}))
    expect(calls).toHaveLength(every.length * 2)
  })

  it("gives the assembled process memory and trace hooks that reach the current turn's own, and nothing between turns (#3453)", async () => {
    const { runner, session, principal } = await turnRunnerForTest()
    const turn = await runner.assemble(session, principal)
    const { slot } = turn
    const memory = turn.run.memoryHooks!
    const trace = turn.run.traceHooks!
    expect(Object.keys(memory).length).toBeGreaterThan(0)
    const calls: string[] = []
    const contextOf = (turnId: string) => {
      const ctx = turnContext(turnId)
      return {
        ...ctx,
        memory: { hooks: recording(memory, turnId, calls), settled: () => Promise.resolve() },
        started: vi.spyOn(ctx.trace, 'toolStarted'),
        ended: vi.spyOn(ctx.trace, 'toolEnded'),
      }
    }
    const fireTrace = async () => {
      await trace.PreToolUse![0]!.hooks[0]!(input('PreToolUse', { tool_use_id: 'toolu_t', tool_name: 'mcp__scadbuddy__get_model', tool_input: {} }), 'toolu_t', hookOptions)
      await trace.PostToolUse![0]!.hooks[0]!(input('PostToolUse', { tool_use_id: 'toolu_t', tool_name: 'mcp__scadbuddy__get_model', tool_input: {}, tool_response: {} }), 'toolu_t', hookOptions)
      await trace.PostToolUseFailure![0]!.hooks[0]!(input('PostToolUseFailure', { tool_use_id: 'toolu_u', tool_name: 'mcp__scadbuddy__get_model', tool_input: {}, error: 'x' }), 'toolu_u', hookOptions)
    }

    // No turn: the hooks answer nothing and reach no one.
    const count = Object.values(memory).flatMap((ms) => ms!.flatMap((m) => m.hooks)).length
    expect(await fireAll(memory)).toEqual(Array(count).fill({}))
    await fireTrace()
    expect(calls).toEqual([])

    const one = contextOf('turn-1')
    slot.set(one)
    const firstAnswers = await fireAll(memory)
    expect(firstAnswers.every((a) => typeof (a as { systemMessage?: string }).systemMessage === 'string' && (a as { systemMessage: string }).systemMessage.startsWith('turn-1 '))).toBe(true)
    await fireTrace()
    expect(one.started.mock.calls).toEqual([['toolu_t', 'mcp__scadbuddy__get_model']])
    expect(one.ended.mock.calls).toEqual([['toolu_t', true], ['toolu_u', false]])

    const two = contextOf('turn-2')
    slot.set(two)
    await fireAll(memory)
    await fireTrace()
    expect(two.started).toHaveBeenCalledTimes(1)
    expect(two.ended).toHaveBeenCalledTimes(2)
    expect(one.started).toHaveBeenCalledTimes(1)
    expect(one.ended).toHaveBeenCalledTimes(2)
    expect(firstAnswers).toHaveLength(count)
    expect(calls.filter((c) => c.startsWith('turn-2 '))).toHaveLength(count)

    slot.clear()
    const before = calls.length
    expect(await fireAll(memory)).toEqual(Array(firstAnswers.length).fill({}))
    await fireTrace()
    expect(calls).toHaveLength(before)
    expect(two.started).toHaveBeenCalledTimes(1)
  })

  it("gives the http tool the slot's turn id and signal as each request reads them, and an aborted signal between turns", () => {
    const slot = createTurnSlot()
    const context = slotHttpContext({ saved: new PgHttpBodies(unusedSql, 'unused'), secrets: () => [], actor: { kind: 'browser', id: 'browser', label: 'You' } }, slot)
    expect(context.turnId).toBeUndefined()
    expect(context.signal?.aborted).toBe(true)
    expect((context.signal?.reason as Error).message).toBe('no turn is running')
    const first = new AbortController()
    slot.set({ ...turnContext('turn-1'), http: { turnId: 'turn-1', signal: first.signal } })
    expect(context.turnId).toBe('turn-1')
    expect(context.signal).toBe(first.signal)
    const second = new AbortController()
    slot.set({ ...turnContext('turn-2'), http: { turnId: 'turn-2', signal: second.signal } })
    expect(context.turnId).toBe('turn-2')
    expect(context.signal).toBe(second.signal)
    slot.clear()
    expect(context.signal?.aborted).toBe(true)
  })

  it('names every HarnessRun key either in the fingerprint or as per-turn', async () => {
    // `sessionId` (a first turn) and `resume` (a later one) never come together,
    // so both assemblies' keys together are every key a run can be given.
    const keys = new Set<string>()
    for (const resume of [false, true]) {
      const { runner, session, principal } = await turnRunnerForTest({ resume })
      // And a Workflow agent's chat, the only one given `effort` and `agent`.
      for (const s of [session, { ...session, agentEffort: 'low' as const, agentType: 'model-author' }]) {
        const turn = await runner.assemble(s, principal)
        for (const key of Object.keys(turn.run)) keys.add(key)
      }
    }
    // `maxRetries` is the one key set after assembly, per attempt (sessions/warmTurn.ts).
    keys.add('maxRetries')
    expect([...keys].sort()).toEqual(TurnRunner.FIXED_AT_SPAWN.slice().sort())
  })
})
