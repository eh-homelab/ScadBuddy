/**
 * The swap wizard's stack of waiting moves (#7168, design 2026-11-27 G3, D6). Each move is a
 * separate request; this module only says which move comes next. No React, no fetch.
 */
import type { Spool } from '../api/filament'

/** The spool and the place it was read in (`null`: in no place). */
export interface Held { spool: Spool; readIn: string | null }
/** A spool that waits for its target to be empty. */
export interface Waiting extends Held { target: string | null }
/** `current` is the spool the person moves now; `waiting[0]` is the oldest, the last is the next. */
export interface Chain { current: Held | null; waiting: Waiting[] }
export type Next =
  | { kind: 'move'; spoolId: string; target: string | null; expectedLocationId: string | null;
      ifOccupied: 'refuse' | 'replace'; expectedOccupantId?: string }
  | { kind: 'name_target' }
  | { kind: 'done' }

const hasTarget = (held: Held): held is Waiting => 'target' in held

const moveOf = (held: Waiting): Next => ({
  kind: 'move',
  spoolId: held.spool.id,
  target: held.target,
  expectedLocationId: held.readIn,
  ifOccupied: 'refuse',
})

const nextFor = (current: Held | null): Next => {
  if (!current) return { kind: 'done' }
  return hasTarget(current) ? moveOf(current) : { kind: 'name_target' }
}

/** The person named the current spool's target: the chain keeps it, and the move goes there. */
export function nameTarget(chain: Chain, target: string | null): { chain: Chain; next: Next } {
  if (!chain.current) return { chain, next: { kind: 'done' } }
  const current: Waiting = { spool: chain.current.spool, readIn: chain.current.readIn, target }
  return { chain: { current, waiting: chain.waiting }, next: moveOf(current) }
}

/** "Move O first": the current spool waits for `target`; `occupant`, read in `target`, becomes current. */
export function moveFirst(chain: Chain, target: string, occupant: Spool): Chain {
  if (!chain.current) return { current: { spool: occupant, readIn: target }, waiting: chain.waiting }
  const waits: Waiting = { spool: chain.current.spool, readIn: chain.current.readIn, target }
  return { current: { spool: occupant, readIn: target }, waiting: [...chain.waiting, waits] }
}

/** "Take W out first": the move of a waiting spool to no place, from where it was read. */
export function takeOutFirst(chain: Chain, waitingSpoolId: string): Next {
  const entry = chain.waiting.find((w) => w.spool.id === waitingSpoolId)
  if (!entry) throw new Error(`spool ${waitingSpoolId} is not waiting`)
  return { kind: 'move', spoolId: entry.spool.id, target: null, expectedLocationId: entry.readIn, ifOccupied: 'refuse' }
}

/**
 * A move succeeded. A waiting spool that moved (taken out first) is now read where it went, and the
 * current move is next again. The current spool that moved leaves the chain, and the newest waiting
 * spool becomes current, its move next.
 */
export function afterMove(chain: Chain, moved: Spool): { chain: Chain; next: Next } {
  const readIn = moved.location_id ?? null
  if (chain.current && chain.current.spool.id === moved.id) {
    const rest = chain.waiting.slice(0, -1)
    const popped = chain.waiting.at(-1) ?? null
    return { chain: { current: popped, waiting: rest }, next: nextFor(popped) }
  }
  const waiting = chain.waiting.map((w) => (w.spool.id === moved.id ? { spool: moved, readIn, target: w.target } : w))
  return { chain: { current: chain.current, waiting }, next: nextFor(chain.current) }
}

/** True when `spoolId` waits in the chain: a move that meets it in a box is a cycle. */
export function isWaiting(chain: Chain, spoolId: string): boolean {
  return chain.waiting.some((w) => w.spool.id === spoolId)
}
