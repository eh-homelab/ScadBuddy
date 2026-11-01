import { describe, expect, it } from 'vitest'
import { createTurnSlot, type TurnContext } from '../src/sessions/turnSlot.js'

// The credential gateway's meter through the slot (gateway/server.ts, plan 2026-10-13 Step 9.4).
describe('a turn slot’s meter', () => {
  const turn = (metered: { usd: number }) => ({ turnId: 't', metered }) as unknown as TurnContext

  it('adds to the running turn, and holds what comes between turns for the next one', () => {
    const slot = createTurnSlot()
    const first = { usd: 0 }
    slot.set(turn(first))
    slot.meter(0.5)
    slot.clear()
    // A command of the first turn still running: no turn to charge now.
    slot.meter(0.25)
    slot.meter(0.25)
    expect(first.usd).toBe(0.5)
    const second = { usd: 0 }
    slot.set(turn(second))
    expect(second.usd).toBe(0.5)
    slot.meter(0.1)
    expect(second.usd).toBeCloseTo(0.6, 12)
    // Given once only.
    slot.clear()
    const third = { usd: 0 }
    slot.set(turn(third))
    expect(third.usd).toBe(0)
  })
})
