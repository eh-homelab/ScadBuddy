import { describe, expect, it } from 'vitest'
import type { ApprovalRequest } from '../src/harness/permissions.js'
import { createTurnSlot } from '../src/sessions/turnSlot.js'
import { TurnRunner } from '../src/sessions/turnRunner.js'
import { turnRunnerForTest } from './support/turnRunner.js'

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

describe('TurnRunner', () => {
  it('assembles callbacks that read the current turn, and deny between turns', async () => {
    const slot = createTurnSlot()
    const { runner, session, principal, gateCalls } = await turnRunnerForTest()
    const turn = await runner.assemble(session, principal, slot)
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

  it('names every HarnessRun key either in the fingerprint or as per-turn', async () => {
    // `sessionId` (a first turn) and `resume` (a later one) never come together,
    // so both assemblies' keys together are every key a run can be given.
    const keys = new Set<string>()
    for (const resume of [false, true]) {
      const { runner, session, principal } = await turnRunnerForTest({ resume })
      const turn = await runner.assemble(session, principal, createTurnSlot())
      for (const key of Object.keys(turn.run)) keys.add(key)
    }
    expect([...keys].sort()).toEqual(TurnRunner.FIXED_AT_SPAWN.slice().sort())
  })
})
