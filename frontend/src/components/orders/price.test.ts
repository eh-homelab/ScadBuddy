import { describe, expect, it } from 'vitest'
import { byCreated } from './price'

describe('byCreated', () => {
  it('orders by instant, not text: a whole second sorts before its own fraction', () => {
    const whole = { id: 'b', created_at: '2026-10-24T23:00:00Z' }
    const later = { id: 'a', created_at: '2026-10-24T23:00:00.123Z' }
    expect([later, whole].sort(byCreated).map((q) => q.id)).toEqual(['b', 'a'])
  })

  it('breaks a tie by id', () => {
    const at = '2026-10-24T23:00:00Z'
    expect([{ id: 'q2', created_at: at }, { id: 'q1', created_at: at }].sort(byCreated).map((q) => q.id)).toEqual(['q1', 'q2'])
  })
})
