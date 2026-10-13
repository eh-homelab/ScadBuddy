import { describe, expect, it } from 'vitest'
import type { ApprovalRequest } from '../src/harness/permissions.js'
import { ROOT_CONTEXT } from '@opentelemetry/api'
import { createTurnSlot, slotHttpContext, type TurnContext } from '../src/sessions/turnSlot.js'
import { type FingerprintParts, TurnRunner } from '../src/sessions/turnRunner.js'
import { TurnTrace } from '../src/telemetry/turn.js'
import { turnRunnerForTest, type TurnRunnerTestOptions } from './support/turnRunner.js'

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

  it("gives the http tool the slot's turn id and signal as each request reads them, and an aborted signal between turns", () => {
    const slot = createTurnSlot()
    const context = slotHttpContext({ saveDir: '/tmp/http', secrets: () => [], actor: { kind: 'browser', id: 'browser', label: 'You' } }, slot)
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
      const turn = await runner.assemble(session, principal)
      for (const key of Object.keys(turn.run)) keys.add(key)
    }
    // `maxRetries` is the one key set after assembly, per attempt (sessions/warmTurn.ts).
    keys.add('maxRetries')
    expect([...keys].sort()).toEqual(TurnRunner.FIXED_AT_SPAWN.slice().sort())
  })
})
