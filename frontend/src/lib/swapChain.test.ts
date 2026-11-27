import { describe, expect, it } from 'vitest'

import type { Spool } from '../api/filament'
import { afterMove, isWaiting, moveFirst, nameTarget, takeOutFirst, type Chain } from './swapChain'

const spool = (id: string, locationId: string | null) => ({ id, location_id: locationId }) as unknown as Spool

const A = 'loc-a'
const B = 'loc-b'
const SHELF = 'loc-shelf'

describe('swapChain', () => {
  it('sends the waiting spool into the box once its occupant moved out', () => {
    const s = spool('s', A)
    const o = spool('o', B)
    const start: Chain = { current: { spool: s, readIn: A }, waiting: [] }
    const chain = moveFirst(start, B, o)
    expect(chain.current).toEqual({ spool: o, readIn: B })
    expect(chain.waiting).toEqual([{ spool: s, readIn: A, target: B }])

    const { next } = afterMove(chain, spool('o', SHELF))
    expect(next).toEqual({ kind: 'move', spoolId: 's', target: B, expectedLocationId: A, ifOccupied: 'refuse' })
  })

  it('asks for the target of a spool that has none', () => {
    const chain = moveFirst({ current: { spool: spool('s', A), readIn: A }, waiting: [] }, B, spool('o', B))
    expect(afterMove(chain, spool('s', null)).next).toEqual({ kind: 'name_target' })
  })

  it('breaks a cycle by taking the waiting spool out first', () => {
    const s = spool('s', A)
    const o = spool('o', B)
    let chain = moveFirst({ current: { spool: s, readIn: A }, waiting: [] }, B, o)
    const aimed = nameTarget(chain, A)
    chain = aimed.chain
    expect(aimed.next).toEqual({ kind: 'move', spoolId: 'o', target: A, expectedLocationId: B, ifOccupied: 'refuse' })

    // O's move into A meets S there.
    expect(isWaiting(chain, 's')).toBe(true)
    expect(isWaiting(chain, 'o')).toBe(false)
    expect(takeOutFirst(chain, 's')).toEqual({ kind: 'move', spoolId: 's', target: null, expectedLocationId: A, ifOccupied: 'refuse' })

    const out = afterMove(chain, spool('s', null))
    expect(out.chain.waiting).toEqual([{ spool: spool('s', null), readIn: null, target: B }])
    expect(out.next).toEqual({ kind: 'move', spoolId: 'o', target: A, expectedLocationId: B, ifOccupied: 'refuse' })

    const last = afterMove(out.chain, spool('o', A))
    expect(last.next).toEqual({ kind: 'move', spoolId: 's', target: B, expectedLocationId: null, ifOccupied: 'refuse' })
    expect(last.chain.waiting).toEqual([])
  })

  it('is done when nothing waits', () => {
    const chain: Chain = { current: { spool: spool('s', A), readIn: A }, waiting: [] }
    const { chain: after, next } = afterMove(chain, spool('s', B))
    expect(next).toEqual({ kind: 'done' })
    expect(after).toEqual({ current: null, waiting: [] })
  })

  it('takes out only a spool that waits', () => {
    const chain: Chain = { current: { spool: spool('s', A), readIn: A }, waiting: [] }
    expect(() => takeOutFirst(chain, 'x')).toThrow()
  })
})
