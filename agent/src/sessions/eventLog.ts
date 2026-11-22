import { EventEmitter } from 'node:events'
import type { AppendHook } from './busEvents.js'
import type { ServerEvent } from './protocol.js'

// Each session's panel-protocol events, in order, in `ai_session_events`
// (#300). Attach (manager.ts) replays this log and then follows it, so a
// watcher sees exactly the sequence a live watcher saw, whichever replica ran
// the turn, and can resume from the last `seq` it has (the "resumability
// event log" of spec §9).
//
// Ordering without gaps: `seq` comes from `ai_sessions.event_seq`, bumped in
// the same statement that inserts the events. The UPDATE holds the session
// row's lock until commit, so two writers of one session commit in seq order
// and a reader that has seen seq N never later finds a smaller one appear.
//
// Going live: a write on THIS replica wakes local followers at once. A write
// on another replica is announced on the event bus (spec §7, #300): `onAppend`
// publishes it as `session.*` (busEvents.ts `SessionEventPublisher`), and every
// replica's change-stream consumer calls `wake()` (busEvents.ts
// `followSessionEvents`). Polling every `pollMs` stays as the fallback for a
// NOTIFY that never arrives (a dropped change stream, none at all; #5299 D4).

// A fork is copy-on-write for its log (#4212): its `ai_sessions` row names the session it was
// forked from (`fork_parent_id`) and the seq of the last event of that session's log, as that
// session reads it, it takes (`fork_event_cut`). Its own `session.started` is its seq 1, and its
// own later events come after the cut (`forkLog`), so `read` is the parent's conversation
// events through the cut (for a fork of a fork, the parent's own read, through the smallest cut on
// the chain) in among the child's own rows, by seq, each addressed to the child. Nothing is copied
// at the fork. Forgetting a parent copies each child's range into the child first
// (`copyForkEvents`, store.ts `copyForkRanges`), at the same seqs, and clears the link. A fork made
// before the cut has its own copy and no `fork_event_cut`, and the chain stops there.

export type LoggedEvent = { seq: number; event: ServerEvent }

/** The conversation events a fork takes; lifecycle events (status, owner, result) are the parent's own. */
export const FORK_EVENT_TYPES: readonly ServerEvent['type'][] = ['user.turn', 'assistant.text.delta', 'assistant.text.done', 'tool.call', 'tool.result']

export const DEFAULT_POLL_MS = 1000
export const PAGE = 500

/** The placement that writes a turn's events (turnHost.ts): the row still names it, checked `FOR UPDATE` in the append's transaction. */
export type TurnFence = { attempt: string; turnId: string }

/** A turn's own events from its `user.turn` on (flowSessions.ts `turnReader`): whether the session stands, the seq read through. */
export type TurnEvents = { here: boolean; after: string | undefined; events: ServerEvent[] }

/** A new fork's log could not be written: its parent was forgotten since the fork's insert. */
export class ForkParentGone extends Error {}

/** A part of an append that has committed: its events and their seqs. */
export type Committed = (events: readonly ServerEvent[], seqs: readonly number[]) => void

/**
 * Where a session's log is kept (#5299 step 9, part b): the API's CRUD routes (`crudEvents.ts`
 * `CrudEventRows`, area `session-events`), each method one transaction there.
 */
export interface EventRows {
  /**
   * The events appended, their seqs; with `fence` only while the row names that placement
   * (undefined otherwise). `committed` hears each part as its transaction commits: all of them at
   * a batch at a time, so a refusal after some batches still tells the
   * followers of those.
   */
  append(sessionId: string, events: readonly ServerEvent[], fence?: TurnFence, committed?: Committed): Promise<number[] | undefined>
  read(sessionId: string, afterSeq: number, limit: number): Promise<LoggedEvent[]>
  userTurnText(sessionId: string, turnId: string): Promise<string | undefined>
  lastSeq(sessionId: string): Promise<number>
  turnEvents(sessionId: string, turnId: string, after: string | undefined): Promise<TurnEvents>
  /**
   * A new fork's own `started`, its parent's log taken through `cut`, then its own `then`, in one
   * transaction under the parent's payload-key lock, shared; their seqs. `ForkParentGone` once the
   * parent was forgotten since the fork's insert.
   */
  forkLog(childId: string, parentId: string, cut: number, started: readonly ServerEvent[], then: readonly ServerEvent[]): Promise<number[]>
}

export class EventLog {
  private readonly rows: EventRows
  private readonly pollMs: number
  private readonly emitter = new EventEmitter()
  /**
   * Per session with at least one local follower: how many follow it, and a
   * counter bumped on every wake, so a follower cannot miss a wake between its
   * read and its wait. The entry exists only while a follower does (created
   * by the first, deleted by the last), so the map is bounded by the number
   * of sessions being watched on this replica, not by every session it has
   * ever written. A wake with no entry has nobody to wake; a follower that
   * starts later reads the table first, so it cannot miss those events.
   */
  private readonly watched = new Map<string, { followers: number; version: number }>()
  /** Told of every append once it has committed (busEvents.ts, the `session.*` publisher). */
  private readonly onAppend: AppendHook | undefined

  /** `rows` is where the log is kept. */
  constructor(rows: EventRows, options: { pollMs?: number; onAppend?: AppendHook } = {}) {
    this.rows = rows
    this.pollMs = options.pollMs ?? DEFAULT_POLL_MS
    this.onAppend = options.onAppend
    this.emitter.setMaxListeners(0)
  }

  /**
   * Appends events to a session's log; returns their seqs. The session row must exist. An
   * edit's, a turn's claim and end, and the gate's write theirs in their own route's transaction
   * (#6449) and then call `committed`.
   */
  async append(sessionId: string, events: readonly ServerEvent[]): Promise<number[]> {
    if (events.length === 0) return []
    return (await this.rows.append(sessionId, events, undefined, (part, seqs) => this.committed(sessionId, part, seqs))) ?? []
  }

  /**
   * A turn's events, only while the row names its placement (`fence`, checked `FOR UPDATE` in the
   * append's transaction); committed once written. Whether they were written.
   */
  async appendFenced(sessionId: string, events: readonly ServerEvent[], fence: TurnFence): Promise<boolean> {
    if (events.length === 0) return true
    const seqs = await this.rows.append(sessionId, events, fence, (part, written) => this.committed(sessionId, part, written))
    return seqs !== undefined
  }

  /**
   * A new fork's log (`EventRows.forkLog`), committed once written: its own `started`, its
   * parent's log through `cut`, then its own `then`.
   */
  async forkLog(childId: string, parentId: string, cut: number, started: readonly ServerEvent[], then: readonly ServerEvent[]): Promise<number[]> {
    const seqs = await this.rows.forkLog(childId, parentId, cut, started, then)
    this.committed(childId, [...started, ...then], seqs)
    return seqs
  }

  /** A turn's own events from its `user.turn` on, past `after` (flowSessions.ts). */
  turnEvents(sessionId: string, turnId: string, after: string | undefined): Promise<TurnEvents> {
    return this.rows.turnEvents(sessionId, turnId, after)
  }

  /**
   * An append has committed: wakes this replica's followers and announces it on
   * the bus (`onAppend`). `append` calls it itself; a caller whose route wrote events calls it
   * once that route has answered.
   */
  committed(sessionId: string, events: readonly ServerEvent[], seqs: readonly number[]): void {
    this.wake(sessionId)
    this.onAppend?.(sessionId, events, seqs.at(-1) ?? 0)
  }

  /**
   * Announces a change to the session row that is not a transcript event (a
   * handoff offer, manager.ts) on the bus, as a batch of `events` at the log's
   * current seq; nothing is appended, so local followers have nothing new.
   */
  async announce(sessionId: string, events: readonly ServerEvent[]): Promise<void> {
    if (!this.onAppend) return
    this.onAppend(sessionId, events, await this.lastSeq(sessionId))
  }

  /** Events after `afterSeq`, oldest first: a fork's through its chain too. */
  async read(sessionId: string, afterSeq = 0, limit = PAGE): Promise<LoggedEvent[]> {
    return this.rows.read(sessionId, afterSeq, limit)
  }

  /**
   * The words of a turn's `user.turn` (the last one logged with that `turnId`), or
   * undefined when none is. One round trip in place of reading the log (#2419). The
   * JSON is not indexed, so Postgres still parses every row of the session to find
   * it: the read's cost still grows with the session, only the transfer does not.
   */
  async userTurnText(sessionId: string, turnId: string): Promise<string | undefined> {
    return this.rows.userTurnText(sessionId, turnId)
  }

  /**
   * The last seq handed out for a session (0 when it has none), to follow it
   * from "now". `sessionId` must be a session's id (a UUID).
   */
  async lastSeq(sessionId: string): Promise<number> {
    return this.rows.lastSeq(sessionId)
  }

  /**
   * Replays the log after `afterSeq`, then follows it until `signal` aborts
   * (or the consumer stops iterating).
   */
  async *follow(sessionId: string, afterSeq = 0, signal?: AbortSignal): AsyncGenerator<LoggedEvent> {
    const entry = this.watched.get(sessionId) ?? { followers: 0, version: 0 }
    entry.followers += 1
    this.watched.set(sessionId, entry)
    try {
      let last = afterSeq
      while (!signal?.aborted) {
        const version = entry.version
        const rows = await this.read(sessionId, last)
        for (const row of rows) {
          if (signal?.aborted) return
          yield row
          last = row.seq
        }
        if (rows.length === PAGE) continue
        await this.waitForChange(sessionId, entry, version, signal)
      }
    } finally {
      // Runs on abort, on the consumer's break/return, and on a read error.
      entry.followers -= 1
      if (entry.followers === 0 && this.watched.get(sessionId) === entry) this.watched.delete(sessionId)
    }
  }

  /** Sessions with a local follower right now (for tests and metrics). */
  watchedSessions(): number {
    return this.watched.size
  }

  /**
   * Wakes this replica's followers of a session: after a local append, and
   * from the change-stream consumer for `session.*` from other replicas
   * (busEvents.ts `followSessionEvents`); the poll then only backs it up.
   */
  wake(sessionId: string): void {
    const entry = this.watched.get(sessionId)
    if (!entry) return
    entry.version += 1
    this.emitter.emit(sessionId)
  }

  /** Wakes every local follower: the bus may have missed events (a resync, a reconnect). */
  wakeAll(): void {
    for (const id of [...this.watched.keys()]) this.wake(id)
  }

  private waitForChange(
    sessionId: string,
    entry: { version: number },
    seen: number,
    signal?: AbortSignal,
  ): Promise<void> {
    if (entry.version !== seen || signal?.aborted) return Promise.resolve()
    return new Promise((resolve) => {
      const done = () => {
        clearTimeout(timer)
        this.emitter.off(sessionId, done)
        signal?.removeEventListener('abort', done)
        resolve()
      }
      const timer = setTimeout(done, this.pollMs)
      this.emitter.on(sessionId, done)
      signal?.addEventListener('abort', done, { once: true })
    })
  }
}
