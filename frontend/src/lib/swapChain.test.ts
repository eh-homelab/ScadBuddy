import { describe, expect, it } from 'vitest'

import type { Spool } from '../api/filament'
import { afterMove, isWaiting, moveFirst, nameTarget, reread, takeOutFirst, type Chain } from './swapChain'

const spool = (id: string, locationId: string | null) => ({ id, location_id: locationId }) as unknown as Spool

const A = 'loc-a'
const B = 'loc-b'
const SHELF = 'loc-shelf'
const C = 'loc-c'

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

  it('moves the waiting spool first, then asks for the target of the occupant that has none', () => {
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

  it('breaks a cycle of three by taking the first spool out', () => {
    let chain = moveFirst({ current: { spool: spool('s', A), readIn: A }, waiting: [] }, B, spool('o', B))
    chain = nameTarget(chain, C).chain
    chain = moveFirst(chain, C, spool('p', C))
    chain = nameTarget(chain, A).chain
    expect(isWaiting(chain, 's')).toBe(true)
    expect(takeOutFirst(chain, 's')).toEqual({ kind: 'move', spoolId: 's', target: null, expectedLocationId: A, ifOccupied: 'refuse' })

    const s1 = afterMove(chain, spool('s', null))
    expect(s1.next).toEqual({ kind: 'move', spoolId: 'p', target: A, expectedLocationId: C, ifOccupied: 'refuse' })
    const s2 = afterMove(s1.chain, spool('p', A))
    expect(s2.next).toEqual({ kind: 'move', spoolId: 'o', target: C, expectedLocationId: B, ifOccupied: 'refuse' })
    const s3 = afterMove(s2.chain, spool('o', C))
    expect(s3.next).toEqual({ kind: 'move', spoolId: 's', target: B, expectedLocationId: null, ifOccupied: 'refuse' })
    expect(afterMove(s3.chain, spool('s', B)).next).toEqual({ kind: 'done' })
  })

  it('takes in a re-read of the current spool and repeats its move with the new place', () => {
    const chain = nameTarget(moveFirst({ current: { spool: spool('s', A), readIn: A }, waiting: [] }, B, spool('o', B)), A).chain
    const { chain: after, next } = reread(chain, spool('o', SHELF))
    expect(after.current).toEqual({ spool: spool('o', SHELF), readIn: SHELF, target: A })
    expect(after.waiting).toEqual(chain.waiting)
    expect(next).toEqual({ kind: 'move', spoolId: 'o', target: A, expectedLocationId: SHELF, ifOccupied: 'refuse' })
  })

  it('asks for the target again after a re-read of a current spool that has none', () => {
    const chain: Chain = { current: { spool: spool('s', A), readIn: A }, waiting: [] }
    const { chain: after, next } = reread(chain, spool('s', B))
    expect(after.current).toEqual({ spool: spool('s', B), readIn: B })
    expect(next).toEqual({ kind: 'name_target' })
  })

  it('takes in a re-read of a waiting spool in the middle and keeps the order', () => {
    let chain = moveFirst({ current: { spool: spool('s', A), readIn: A }, waiting: [] }, B, spool('o', B))
    chain = nameTarget(chain, C).chain
    chain = moveFirst(chain, C, spool('p', C))
    const { chain: after, next } = reread(chain, spool('o', SHELF))
    expect(after.waiting.map((w) => w.spool.id)).toEqual(['s', 'o'])
    expect(after.waiting[1]).toEqual({ spool: spool('o', SHELF), readIn: SHELF, target: C })
    expect(after.current).toEqual(chain.current)
    expect(next).toEqual({ kind: 'name_target' })
  })

  it('reads a re-read spool with no place as null', () => {
    const chain: Chain = { current: { spool: spool('s', A), readIn: A }, waiting: [] }
    expect(reread(chain, spool('s', null)).chain.current?.readIn).toBeNull()
  })

  it('refuses a re-read of an unknown spool', () => {
    const chain: Chain = { current: { spool: spool('s', A), readIn: A }, waiting: [] }
    expect(() => reread(chain, spool('x', B))).toThrow()
  })

  it('refuses a move result for a spool that is neither current nor waiting', () => {
    const chain: Chain = { current: { spool: spool('s', A), readIn: A }, waiting: [] }
    expect(() => afterMove(chain, spool('x', B))).toThrow()
  })

  it('refuses an occupant that already waits', () => {
    const chain = moveFirst({ current: { spool: spool('s', A), readIn: A }, waiting: [] }, B, spool('o', B))
    expect(() => moveFirst(chain, A, spool('s', A))).toThrow()
  })

  it('refuses to move first when no spool is current', () => {
    expect(() => moveFirst({ current: null, waiting: [] }, B, spool('o', B))).toThrow()
  })
})
