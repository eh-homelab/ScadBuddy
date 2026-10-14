// Warm Claude Code processes (unified runtime spec 2026-10-13 §5.3, plan Phase 2).
// A process is reused only while the fingerprint of everything fixed at its spawn
// still matches the turn's.

import { createHash } from 'node:crypto'
import type {
  Query,
  SDKMessage,
  SDKResultMessage,
  SessionKey,
  SessionStore,
  SessionStoreEntry,
} from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'
import type { Credential } from '../credentials.js'
import { carries } from '../harness/fallback.js'
import { Inbox } from '../harness/inbox.js'
import { type HarnessRun, runHarness } from '../harness/run.js'
import type { PostgresSessionStore } from './store.js'
import type { AssembledTurn, FingerprintParts } from './turnRunner.js'

type Json = null | boolean | number | string | Json[] | { [key: string]: Json }

/** Object keys sorted at every depth; arrays sorted (primitives by value, objects by their canonical JSON), so order never matters. */
function canonical(value: unknown): Json {
  if (Array.isArray(value)) {
    return value
      .map((item) => canonical(item))
      .sort((a, b) => {
        const x = JSON.stringify(a)
        const y = JSON.stringify(b)
        return x < y ? -1 : x > y ? 1 : 0
      })
  }
  if (value !== null && typeof value === 'object') {
    const out: { [key: string]: Json } = {}
    for (const key of Object.keys(value).sort()) {
      const item = (value as Record<string, unknown>)[key]
      if (item !== undefined) out[key] = canonical(item)
    }
    return out
  }
  return (value ?? null) as Json
}

/** The sha256 hex of the parts' canonical JSON. The parts hold no secret in clear (plugin header versions are HMACs). */
export function fingerprint(parts: FingerprintParts): string {
  return createHash('sha256').update(JSON.stringify(canonical(parts))).digest('hex')
}

export type WarmOutcome = 'hit' | 'miss' | 'recycle'
export type RecycleReason = 'fingerprint' | 'tail' | 'budget' | 'evicted'

/**
 * What the cache needs of a started query: its messages, `interrupt()` (a turn
 * ends through it, spec §5.3) and `close()` (the process ends through it). The
 * SDK's Query is one; sessions/manager.ts makes one of a scripted stand-in.
 */
export type WarmQuery = AsyncIterable<SDKMessage> & Pick<Query, 'interrupt' | 'close'>

/** One warm Claude Code process: a streaming-input query and the assembly that spawned it (spec §5.3). */
export type WarmEntry = {
  readonly sessionId: string
  readonly query: WarmQuery
  /** What each turn pushes its user message into. */
  readonly inbox: Inbox
  /** The query's one iterator: a turn reads it up to its `result`. */
  readonly iterator: AsyncIterator<SDKMessage>
  /** The assembly the process was spawned with: its callbacks, forwarder routes and package dirs are the ones it runs with. */
  readonly turn: AssembledTurn
  readonly fingerprint: string
  /** `maxBudgetUsd` at spawn: Claude Code counts it against the spend since its query started (E5, §2.2). */
  readonly capUsd: number
  /** The process's `total_cost_usd` before its first turn: what the transcript carried in, or 0. */
  costAtSpawnUsd: number
  /** The `total_cost_usd` of the last settled turn: cumulative for the process (§2.2). */
  lastTotalUsd: number
  lastUsedAt: number
}

export type WarmTurn = { entry: WarmEntry; outcome: WarmOutcome; reason?: RecycleReason }

export type WarmSessionsDeps = {
  sql: Sql
  /** The transcript store: its last `cost-state` is the baseline a resume restores. */
  store: Pick<PostgresSessionStore, 'lastOfType'>
  /** `warm_max_per_replica` (D3): past it the least recently used idle entry goes. */
  maxEntries: number
  /** `warm_idle_seconds`, in ms: an entry idle this long goes. */
  idleMs: number
  /** Starts each process's query; `runHarness` (the real Claude Code) when omitted. */
  start?: (run: HarnessRun) => WarmQuery
  now?: () => number
  log?: (message: string) => void
}

/** USD: how far the remaining budget may sit from the process's cap minus its spend and still match (§5.3). */
const BUDGET_EPSILON = 1e-6
/** The least cap a spawn is given, as TurnRunner's (`maxBudgetUsd` must be positive). */
const MIN_CAP_USD = 0.000001
/**
 * How long a live process that is being closed gets to end on its own once its
 * stdin is closed, and its last transcript appends to land, before `close()`.
 * Measured against the fake endpoint (test/warm.pg.test.ts, the bundled Claude
 * Code): nothing is written after the `result` while the process idles; on
 * stdin EOF it writes `last-prompt` and `cost-state` and its stream ends within
 * about 50 ms. `close()` at once stops the transport handing appends over, and
 * those are lost (the flake behind #1093's fallback test).
 */
const DRAIN_MS = 2_000
/**
 * How long a reservation (`reserve`) is its session's alone: its `session_turn` may still be
 * on its way (the workflow's 5 s schedule-to-start, then the spawn), so it is not given up for
 * another session's until then (spec 2026-10-13 §5.2).
 */
export const RESERVATION_HOLD_MS = 30_000

/** `acquire` while a turn holds the session's entry: the caller runs one turn per session at a time, so this is its bug. */
export class WarmBusyError extends Error {
  override name = 'WarmBusyError'
  constructor(sessionId: string) {
    super(`session ${sessionId} already has a turn running on its warm process`)
  }
}

/**
 * The service's transcript mirror as one process sees it: every append goes
 * through to `inner`, in call order, and the last main-transcript uuid that
 * landed is remembered. The transcript tail rule compares it with the table's
 * tail: equal unless another process wrote since. A process's own writes can
 * land after its `result` (a `prompt_snapshot` attachment and `last-prompt`
 * did; a streaming process's `cost-state` only as it ends: measured against
 * the fake endpoint, test/warm.pg.test.ts), so the rule never compares a tail
 * read at the result with a later one. And since another process's write
 * could land just before one of this process's own, which would then be the
 * tail again, each of its own appends first checks that the table's tail is
 * still the last one it wrote (`foreign`).
 */
class MirroredTranscript {
  readonly store: SessionStore
  lastUuid: string | null
  /** Every append so far, in order; never rejects. */
  pending: Promise<void> = Promise.resolve()
  /** Another write landed in the main transcript between two of this process's own. */
  foreign = false
  /** Cut off (`WarmSessions.cut`): a placement the workflow gave up on; nothing more of it is written. */
  fenced = false

  constructor(inner: SessionStore, tail: string | null, readTail: () => Promise<string | null>) {
    this.lastUuid = tail
    const append = (key: SessionKey, entries: SessionStoreEntry[]): Promise<void> => {
      const main = (key.subpath ?? '') === ''
      const uuid = main ? entries.findLast((e) => typeof e.uuid === 'string')?.uuid : undefined
      const write = this.pending.then(async () => {
        if (this.fenced) return
        if (typeof uuid === 'string' && !this.foreign) {
          // A failed read cannot tell: taken as moved, which only costs a respawn.
          const tail = await readTail().catch(() => undefined)
          if (tail !== this.lastUuid) this.foreign = true
        }
        await inner.append(key, entries)
      })
      this.pending = write.then(
        () => {
          if (typeof uuid === 'string') this.lastUuid = uuid
        },
        // A failed append is the SDK's to retry; the tail stays where it landed.
        () => {},
      )
      return write
    }
    this.store = {
      append,
      load: (key) => inner.load(key),
      ...(inner.listSessions ? { listSessions: inner.listSessions.bind(inner) } : {}),
      ...(inner.listSessionSummaries ? { listSessionSummaries: inner.listSessionSummaries.bind(inner) } : {}),
      ...(inner.delete ? { delete: inner.delete.bind(inner) } : {}),
      ...(inner.listSubkeys ? { listSubkeys: inner.listSubkeys.bind(inner) } : {}),
    }
  }
}

type Slot = {
  entry: WarmEntry
  /** sha256 of the credential it was spawned with (`credentialHash`): the secret itself is not kept. */
  credential: string
  /** The pool candidate it was spawned on, when the turn's candidates held it: what a resync looks for again. */
  candidate: { id: string; epoch: number } | undefined
  mirror: MirroredTranscript | undefined
  /** What the transcript's last `cost-state` held at spawn: what a resume restores (manager.ts `finish`). */
  carriedUsd: number
  settled: boolean
  /** From `acquire` (its checks included) to `settle` or `abandon`: never evicted for idleness or the cap. */
  busy: boolean
  /** Evicted while busy: closed at `settle`, or, during `acquire`'s checks, recycled (`evicted`). */
  doomed?: string
  timer?: ReturnType<typeof setTimeout>
  closed: boolean
}

const credentialHash = (c: Credential) =>
  createHash('sha256')
    .update(JSON.stringify([c.kind, c.kind === 'gateway' ? c.baseUrl : '', c.secret]))
    .digest('hex')

/**
 * One warm Claude Code per session on this replica (spec §5.3). `acquire`
 * reuses the session's process only while it still matches the turn: the
 * fingerprint of everything fixed at spawn (and the very credential it was
 * spawned with), the transcript tail, and the budget. Anything else closes it
 * and spawns again, resuming from the transcript (E2). An entry owns the
 * assembly it was spawned with: a hit releases the turn's own, unused one;
 * closing an entry releases its.
 */
export class WarmSessions {
  private readonly deps: WarmSessionsDeps
  private maxEntries: number
  private idleMs: number
  private readonly now: () => number
  private readonly log: (message: string) => void
  private readonly start: (run: HarnessRun) => WarmQuery
  /** By session, least recently used first. */
  private readonly slots = new Map<string, Slot>()
  /** A closed process's transcript appends still in flight, by session: the next spawn waits for them before it loads. */
  private readonly draining = new Map<string, MirroredTranscript>()
  /** Each session's process still closing (`drop`): the next spawn waits for it, and `close()` for all. */
  private readonly closing = new Map<string, Promise<void>>()
  /** Each session's acquire, one at a time: two would spawn two processes. */
  private readonly acquiring = new Map<string, Promise<unknown>>()
  /**
   * Places `reserve` promised to sessions that have no process here yet (spec 2026-10-13 §5.2,
   * `acquire_worker`), by when: each counts against `maxEntries` until its session's process
   * spawns, it is released, or it lapses after `idleMs` like an idle process.
   */
  private readonly reserved = new Map<string, number>()

  constructor(deps: WarmSessionsDeps) {
    this.deps = deps
    this.maxEntries = deps.maxEntries
    this.idleMs = deps.idleMs
    this.now = deps.now ?? Date.now
    this.log = deps.log ?? (() => {})
    this.start = deps.start ?? runHarness
  }

  /**
   * The session's process for this turn, which the turn then holds until
   * `settle` (it ended with a `result`) or `abandon` (any other end). One turn
   * per session at a time: an acquire while one holds the entry throws
   * WarmBusyError and leaves that turn alone. On a throw the assembly is still
   * the caller's to release.
   */
  acquire(sessionId: string, turn: AssembledTurn, credential: Credential, remainingUsd: number): Promise<WarmTurn> {
    const previous = this.acquiring.get(sessionId) ?? Promise.resolve()
    const next = previous.then(() => this.acquireNow(sessionId, turn, credential, remainingUsd))
    const done = next.catch(() => {})
    this.acquiring.set(sessionId, done)
    void done.then(() => {
      if (this.acquiring.get(sessionId) === done) this.acquiring.delete(sessionId)
    })
    return next
  }

  /** After the turn's result: records the process's cumulative cost; the entry is idle again. */
  async settle(entry: WarmEntry, result: SDKResultMessage): Promise<void> {
    const slot = this.slots.get(entry.sessionId)
    if (!slot || slot.entry !== entry) return
    if (!slot.settled) {
      // A resumed process's total starts from the transcript's `cost-state`
      // (§2.2), unless the restore did not happen (fallback.ts `carries`).
      entry.costAtSpawnUsd = carries(result.total_cost_usd, slot.carriedUsd) ? slot.carriedUsd : 0
      slot.settled = true
    }
    entry.lastTotalUsd = result.total_cost_usd
    entry.lastUsedAt = this.now()
    slot.busy = false
    await slot.mirror?.pending
    if (slot.doomed !== undefined) {
      void this.drop(slot, slot.doomed)
      return
    }
    this.arm(slot)
    this.trim()
  }

  /**
   * After a turn that ended without a `result`: its process is closed and its
   * assembly released, busy or not; the next acquire spawns afresh. `close`
   * (the default) is for a process that is dead or was aborted: it is closed
   * at once. `drain` is for one still alive and well, left by the fallback for
   * another credential (sessions/warmTurn.ts): it gets DRAIN_MS to end on its
   * own and land its last appends first, so the transcript the next attempt
   * resumes from is whole.
   */
  abandon(entry: WarmEntry, reason: string, how: 'close' | 'drain' = 'close'): Promise<void> {
    const slot = this.slots.get(entry.sessionId)
    return slot?.entry === entry ? this.drop(slot, `abandoned: ${reason}`, how) : Promise.resolve()
  }

  /**
   * A turn that pushed nothing into the entry it acquired (stopped first) gives
   * it back as it found it: idle, or closed if it was evicted meanwhile.
   */
  leave(entry: WarmEntry): void {
    const slot = this.slots.get(entry.sessionId)
    if (!slot || slot.entry !== entry || !slot.busy) return
    slot.busy = false
    if (slot.doomed !== undefined) {
      void this.drop(slot, slot.doomed)
      return
    }
    this.arm(slot)
    this.trim()
  }

  /**
   * Closes the session's process (`*`: every one), letting it end on its own
   * first (`drop`); settled when every one it closed is closed. One with a
   * turn running is closed when the turn settles.
   */
  evict(sessionId: string | '*', reason: string): Promise<void> {
    const closing: Promise<void>[] = []
    for (const slot of [...this.slots.values()]) {
      if (sessionId !== '*' && slot.entry.sessionId !== sessionId) continue
      if (slot.busy) slot.doomed = reason
      else closing.push(this.drop(slot, reason))
    }
    return Promise.all(closing).then(() => {})
  }

  /**
   * New `warm_max_per_replica` and `warm_idle_seconds` (in ms), applied to the
   * entries there already are: idle ones past the new cap go, least recently
   * used first, and the idle timers are set again. A running turn is never
   * touched; at 0 its process goes when it settles.
   */
  resize(maxEntries: number, idleMs: number): void {
    this.maxEntries = maxEntries
    this.idleMs = idleMs
    for (const slot of this.slots.values()) if (!slot.busy) this.arm(slot)
    this.sweepIdle()
    this.trim()
  }

  /** Closes every process with no turn running, letting it end on its own first (`drop`); a running turn is never touched. */
  evictIdle(reason: string): Promise<void> {
    const closing: Promise<void>[] = []
    for (const slot of [...this.slots.values()]) if (!slot.busy) closing.push(this.drop(slot, reason))
    return Promise.all(closing).then(() => {})
  }

  /**
   * Drops each idle process that a fresh assembly of its session would not
   * reuse it for: the reuse rule of `acquire` (`mismatch`), so a revoked
   * credential or changed tiers end a process that no NOTIFY reached. The
   * credential is the one the process was spawned on (same pool id, epoch and
   * secret), not the pool's first: a healthy process on a fallback stays. The
   * budget is taken as the process expects it. A session `assemble` gives
   * nothing for, or fails to assemble, loses its process: it could not be
   * vouched for. One that is acquired meanwhile is left to its turn.
   */
  async revalidate(assemble: (entry: WarmEntry) => Promise<AssembledTurn | undefined>): Promise<void> {
    for (const slot of [...this.slots.values()]) {
      if (slot.busy || slot.closed) continue
      let reason: string | undefined
      try {
        const fresh = await assemble(slot.entry)
        if (!fresh) reason = 'its session is gone'
        else {
          try {
            const entry = slot.entry
            // The credential it was spawned on, as the pool has it now (a fallback's too), and every other
            // fingerprint input: the pool's first candidate is not this process's business.
            const spawned = fresh.candidates.find(
              (c) =>
                credentialHash(c.credential) === slot.credential &&
                (slot.candidate === undefined || (c.id === slot.candidate.id && c.epoch === slot.candidate.epoch)),
            )
            const apart = (parts: FingerprintParts) => fingerprint({ ...parts, credentialId: '', credentialUpdatedAt: '' })
            // An empty candidate pool (a credential outage) finds none: every idle process goes within one
            // interval, and that is intended: none of them can be vouched for while nothing is usable.
            if (!spawned) reason = 'credential'
            else if (apart(fresh.fingerprintParts) !== apart(entry.turn.fingerprintParts)) reason = 'fingerprint'
            else
              reason = await this.mismatch(slot, entry.fingerprint, spawned.credential, entry.capUsd - (entry.lastTotalUsd - entry.costAtSpawnUsd))
          } finally {
            fresh.release()
          }
        }
      } catch (err) {
        reason = `the check failed: ${err instanceof Error ? err.message : String(err)}`
      }
      if (reason !== undefined && !slot.busy && !slot.closed) void this.drop(slot, `revalidated: ${reason}`)
    }
  }

  /**
   * A place for the session's process on this replica (`acquire_worker`, spec §5.2): kept for a
   * session that has one already, else taken, evicting the least recently used idle process or
   * reservation when the cache is full. `full` only when every process here is running a turn.
   */
  reserve(sessionId: string): 'reserved' | 'full' {
    // No warm processes at all (`warm_max_per_replica` 0): every turn runs cold, and is admitted;
    // the session host worker's slots bound how many run at once.
    if (this.maxEntries <= 0) return 'reserved'
    this.sweepIdle()
    const now = this.now()
    for (const [id, at] of [...this.reserved]) if (now - at >= this.idleMs || this.slots.has(id)) this.reserved.delete(id)
    if (this.slots.has(sessionId)) return 'reserved'
    if (!this.reserved.has(sessionId)) {
      while (this.slots.size + this.reserved.size >= this.maxEntries) {
        let lru: Slot | undefined
        for (const slot of this.slots.values()) {
          if (!slot.busy && (!lru || slot.entry.lastUsedAt < lru.entry.lastUsedAt)) lru = slot
        }
        // A reservation still being placed (its turn on the way) is not given up.
        let oldest: [string, number] | undefined
        for (const held of this.reserved) if (now - held[1] >= RESERVATION_HOLD_MS && (!oldest || held[1] < oldest[1])) oldest = held
        if (!lru && !oldest) return 'full'
        if (oldest && (!lru || oldest[1] < lru.entry.lastUsedAt)) this.reserved.delete(oldest[0])
        else void this.drop(lru!, 'lru')
      }
    }
    this.reserved.set(sessionId, now)
    return 'reserved'
  }

  /** Frees the session's place here (`release_worker`, spec §5.2): its reservation, and its process, once no turn runs on it. */
  release(sessionId: string): Promise<void> {
    this.reserved.delete(sessionId)
    return this.evict(sessionId, 'released')
  }

  size(): number {
    return this.slots.size
  }

  /** Whether a turn holds the session's process here now. */
  busy(sessionId: string): boolean {
    return this.slots.get(sessionId)?.busy === true
  }

  /**
   * Cuts off a placement the workflow gave up on (spec §5.2, a stalled heartbeat): its
   * transcript appends from now on are dropped, and its process is closed at once, busy or not.
   */
  cut(entry: WarmEntry, reason: string): Promise<void> {
    const slot = this.slots.get(entry.sessionId)
    if (!slot || slot.entry !== entry) return Promise.resolve()
    if (slot.mirror) slot.mirror.fenced = true
    return this.drop(slot, `cut off: ${reason}`, 'close')
  }

  /** Closes every process (shutdown): an idle one may end on its own first; a running turn's is closed at once. */
  async close(): Promise<void> {
    this.reserved.clear()
    const slots = [...this.slots.values()]
    await Promise.all(slots.map((slot) => this.drop(slot, 'shutdown', slot.busy ? 'close' : 'drain')))
    await Promise.all(slots.map((slot) => slot.mirror?.pending))
  }

  private async acquireNow(sessionId: string, turn: AssembledTurn, credential: Credential, remainingUsd: number): Promise<WarmTurn> {
    this.sweepIdle()
    const print = fingerprint(turn.fingerprintParts)
    const slot = this.slots.get(sessionId)
    if (slot?.busy) throw new WarmBusyError(sessionId)
    let reason: RecycleReason | undefined
    if (slot) {
      // Held while it is checked: the idle timer, the cap and `evict` leave it be.
      slot.busy = true
      clearTimeout(slot.timer)
      try {
        reason = await this.mismatch(slot, print, credential, remainingUsd)
      } catch (err) {
        // A failed check (the tail read) cannot vouch for the process, and the
        // caller has no entry to settle or abandon: it goes now, or it would stay held.
        slot.busy = false
        void this.drop(slot, `the check failed: ${err instanceof Error ? err.message : String(err)}`, 'close')
        throw err
      }
      // Closed meanwhile (shutdown): nothing to reuse or recycle.
      const gone = slot.closed || this.slots.get(sessionId) !== slot
      if (gone) reason = undefined
      else {
        if (slot.doomed !== undefined) reason = 'evicted'
        if (reason === undefined) {
          turn.release()
          slot.entry.lastUsedAt = this.now()
          this.slots.delete(sessionId)
          this.slots.set(sessionId, slot)
          return { entry: slot.entry, outcome: 'hit' }
        }
        slot.busy = false
        void this.drop(slot, `recycled: ${reason}`)
      }
    }
    // A closed process (recycled, abandoned, evicted) ends, and its last appends land, before the new one loads the transcript.
    await this.closing.get(sessionId)
    await this.draining.get(sessionId)?.pending
    const entry = await this.spawn(sessionId, turn, credential, remainingUsd, print)
    this.trim()
    return reason === undefined ? { entry, outcome: 'miss' } : { entry, outcome: 'recycle', reason }
  }

  /** Why the process cannot serve this turn (§5.3 "Reuse rule"), or undefined when it can. */
  private async mismatch(slot: Slot, print: string, credential: Credential, remainingUsd: number): Promise<RecycleReason | undefined> {
    // The credential is in the process's env (run.ts `credentialEnv`); a
    // fallback to another candidate keeps the fingerprint but not the key.
    if (slot.entry.fingerprint !== print || slot.credential !== credentialHash(credential)) return 'fingerprint'
    if (await this.tailMoved(slot)) return 'tail'
    const entry = slot.entry
    const expected = entry.capUsd - (entry.lastTotalUsd - entry.costAtSpawnUsd)
    if (Math.abs(remainingUsd - expected) >= BUDGET_EPSILON) return 'budget'
    return undefined
  }

  /** Whether a write other than this process's own moved the main transcript's tail. */
  private async tailMoved(slot: Slot): Promise<boolean> {
    const mirror = slot.mirror
    if (!mirror) return false
    // Read once every append the process has handed over has landed; read
    // again if another was handed over while reading.
    for (let attempt = 0; attempt < 3; attempt++) {
      const pending = mirror.pending
      await pending
      const tail = await this.tail(slot.entry.sessionId)
      if (mirror.pending === pending) return mirror.foreign || tail !== mirror.lastUuid
    }
    return true
  }

  private async tail(sessionId: string): Promise<string | null> {
    const rows = await this.deps.sql<{ uuid: string }[]>`
      SELECT uuid FROM ai_session_entries
      WHERE session_id = ${sessionId} AND subpath = '' AND uuid IS NOT NULL
      ORDER BY id DESC LIMIT 1`
    return rows[0]?.uuid ?? null
  }

  private async spawn(sessionId: string, turn: AssembledTurn, credential: Credential, remainingUsd: number, print: string): Promise<WarmEntry> {
    // The tail the process resumes from, whether there is a transcript to
    // resume at all, and the cost a resume restores (its last `cost-state`).
    const [row] = await this.deps.sql<{ tail: string | null; resume: boolean }[]>`
      SELECT
        (SELECT uuid FROM ai_session_entries
          WHERE session_id = ${sessionId} AND subpath = '' AND uuid IS NOT NULL
          ORDER BY id DESC LIMIT 1) AS tail,
        EXISTS (SELECT 1 FROM ai_session_entries WHERE session_id = ${sessionId} AND subpath = '') AS resume`
    const costState = row?.resume ? await this.deps.store.lastOfType({ projectKey: '', sessionId }, 'cost-state') : null
    const carriedUsd = costStateUsd(costState === null ? null : JSON.stringify(costState), this.log)
    const mirror = turn.run.sessionStore
      ? new MirroredTranscript(turn.run.sessionStore, row?.tail ?? null, () => this.tail(sessionId))
      : undefined
    const inbox = new Inbox()
    const { resume: _resume, sessionId: _sessionId, ...run } = turn.run
    const query = this.start({
      ...run,
      credential,
      prompt: inbox,
      maxBudgetUsd: Math.max(remainingUsd, MIN_CAP_USD),
      ...(mirror ? { sessionStore: mirror.store } : {}),
      ...(row?.resume ? { resume: sessionId } : { sessionId }),
    })
    const entry: WarmEntry = {
      sessionId,
      query,
      inbox,
      iterator: query[Symbol.asyncIterator](),
      turn,
      fingerprint: print,
      capUsd: remainingUsd,
      costAtSpawnUsd: carriedUsd,
      lastTotalUsd: carriedUsd,
      lastUsedAt: this.now(),
    }
    const hash = credentialHash(credential)
    const on = turn.candidates.find((c) => credentialHash(c.credential) === hash)
    // Its reserved place (`reserve`) is the process's now.
    this.reserved.delete(sessionId)
    this.slots.set(sessionId, {
      entry,
      credential: hash,
      candidate: on ? { id: on.id, epoch: on.epoch } : undefined,
      mirror,
      carriedUsd,
      settled: false,
      busy: true,
      closed: false,
    })
    return entry
  }

  /** The idle timer (unref'd): it goes once `now` says the entry has been idle for `idleMs`. */
  private arm(slot: Slot): void {
    clearTimeout(slot.timer)
    const check = () => {
      if (slot.closed || slot.busy) return
      const idle = this.now() - slot.entry.lastUsedAt
      if (idle >= this.idleMs) void this.drop(slot, 'idle')
      else schedule(this.idleMs - idle)
    }
    const schedule = (ms: number) => {
      slot.timer = setTimeout(check, ms)
      slot.timer.unref()
    }
    schedule(this.idleMs)
  }

  private sweepIdle(): void {
    for (const slot of [...this.slots.values()]) {
      if (!slot.busy && this.now() - slot.entry.lastUsedAt >= this.idleMs) void this.drop(slot, 'idle')
    }
  }

  /** Past `maxEntries` (reserved places counted), the least recently used idle entries go; never one with a turn running. */
  private trim(): void {
    while (this.slots.size + this.reserved.size > this.maxEntries) {
      let lru: Slot | undefined
      for (const slot of this.slots.values()) {
        if (!slot.busy && (!lru || slot.entry.lastUsedAt < lru.entry.lastUsedAt)) lru = slot
      }
      if (!lru) return
      void this.drop(lru, 'lru')
    }
  }

  /**
   * Closes the process and releases the assembly it was spawned with; settled
   * once both are done. Its stdin is closed first either way. `drain` (the
   * default: a recycle, an eviction, idle, the cap) then gives a live process
   * DRAIN_MS to end on its own and land its last appends (`last-prompt`,
   * `cost-state`) before `close()`; `close` (dead or aborted) closes at once.
   */
  private drop(slot: Slot, reason: string, how: 'drain' | 'close' = 'drain'): Promise<void> {
    if (slot.closed) return Promise.resolve()
    slot.closed = true
    clearTimeout(slot.timer)
    const id = slot.entry.sessionId
    if (this.slots.get(id) === slot) this.slots.delete(id)
    const mirror = slot.mirror
    if (mirror) {
      // Kept until its appends, those handed over after the close too, have landed.
      this.draining.set(id, mirror)
      const forget = () => {
        const pending = mirror.pending
        void pending.then(() => {
          if (mirror.pending !== pending) forget()
          else if (this.draining.get(id) === mirror) this.draining.delete(id)
        })
      }
      forget()
    }
    slot.entry.inbox.close()
    // The process has ended once `ended` settles: the next spawn waits for that and no more.
    const ended = (how === 'drain' ? this.drain(slot) : Promise.resolve()).then(async () => {
      try {
        // `void` in the SDK's type; a rejection at run time is this log's, not an unhandled one.
        await slot.entry.query.close()
      } catch (err) {
        this.log(`warm: closing session ${id}'s process failed: ${err instanceof Error ? err.message : String(err)}`)
      }
    })
    this.closing.set(id, ended)
    void ended.then(() => {
      if (this.closing.get(id) === ended) this.closing.delete(id)
    })
    // Its plugin sessions go after it (it sent no DELETE of its own), outside `closing`: up to 5 s that a spawn does not wait for.
    return ended.then(async () => {
      try {
        await slot.entry.turn.terminate()
      } catch (err) {
        this.log(`warm: ending session ${id}'s plugin sessions failed: ${err instanceof Error ? err.message : String(err)}`)
      }
      try {
        slot.entry.turn.release()
      } catch (err) {
        // A route or directory that would not go is the forwarder's or the
        // package cache's to report; the process is closed and the entry gone.
        this.log(`warm: releasing session ${id}'s assembly failed: ${err instanceof Error ? err.message : String(err)}`)
      }
      this.log(`warm: closed session ${id}'s process: ${reason}`)
    })
  }

  /**
   * Waits, up to DRAIN_MS in all, for the process's stream to end (its stdin is
   * closed) and then for its appends so far to land. It reads the Query only
   * once the turn that held the entry has left it (`drop` runs for an idle
   * entry, or after the fallback returned the attempt's iterator): one reader
   * at a time, so no turn's message is taken here.
   */
  private async drain(slot: Slot): Promise<void> {
    let timer: ReturnType<typeof setTimeout> | undefined
    const deadline = new Promise<void>((resolve) => {
      timer = setTimeout(resolve, DRAIN_MS)
      timer.unref()
    })
    const ended = (async () => {
      for (;;) {
        const next = await slot.entry.iterator.next()
        if (next.done) return
      }
    })().catch(() => {})
    await Promise.race([ended, deadline])
    const mirror = slot.mirror
    if (mirror) {
      const landed = (async () => {
        for (;;) {
          const pending = mirror.pending
          await pending
          if (mirror.pending === pending) return
        }
      })()
      await Promise.race([landed, deadline])
    }
    clearTimeout(timer)
  }
}

/**
 * What a turn that ended with `result` cost on `entry`'s process: the
 * process's total less what it stood at before the turn (`total_cost_usd` is
 * cumulative per process, §2.2), taken before `settle` moves it. For the
 * first result of a resumed process the baseline is the transcript's carried
 * cost, unless the restore did not happen (`carries`, as `settle` judges it):
 * then the total is the turn's own.
 */
export function turnCostUsd(entry: WarmEntry, result: SDKResultMessage): number {
  const before = entry.lastTotalUsd
  return carries(result.total_cost_usd, before) ? Math.max(result.total_cost_usd - before, 0) : result.total_cost_usd
}

/**
 * The `totalCostUSD` of a `cost-state` transcript entry; 0 when there is none.
 * A row it cannot read is no baseline either, said in `log`: it must not stop
 * the spawn (a wrong baseline only costs a `budget` respawn).
 */
export function costStateUsd(entry: string | null, log: (message: string) => void): number {
  if (entry === null) return 0
  let parsed: { type?: unknown; totalCostUSD?: unknown }
  try {
    parsed = JSON.parse(entry) as typeof parsed
  } catch (err) {
    log(`warm: a cost-state transcript entry is not JSON (${(err as Error).message}); counting from 0`)
    return 0
  }
  if (parsed.type === 'cost-state' && typeof parsed.totalCostUSD === 'number') return parsed.totalCostUSD
  log('warm: a cost-state transcript entry has no numeric totalCostUSD; counting from 0')
  return 0
}
