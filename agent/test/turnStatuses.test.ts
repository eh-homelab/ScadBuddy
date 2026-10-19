import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'
import { TURN_STATUSES } from '../src/sessions/durableSweep.js'

// The shared list of a turn's statuses: the same file pins agent-durable's
// `_RUNNING` and `_WAITING` (tests/test_turn_statuses.py).
const doc = JSON.parse(readFileSync(new URL('./fixtures/turn-statuses.json', import.meta.url), 'utf8')) as {
  turn: string[]
  waiting: string[]
}

it("sweeps exactly the statuses agent-durable's session holds a turn in", () => {
  expect([...TURN_STATUSES]).toEqual(doc.turn)
})

it('keeps every parked status among the turn statuses', () => {
  expect(doc.waiting.length).toBeGreaterThan(0)
  expect(doc.waiting.filter((s) => !TURN_STATUSES.includes(s))).toEqual([])
})
