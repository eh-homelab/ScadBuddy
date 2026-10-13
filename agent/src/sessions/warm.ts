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
import { runHarness } from '../harness/run.js'
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

/** One warm Claude Code process: a streaming-input query and the assembly that spawned it (spec §5.3). */
export type WarmEntry = {
  readonly sessionId: string
  readonly query: Query
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
  /** `warm_max_per_replica` (D3): past it the least recently used idle entry goes. */
  maxEntries: number
  /** `warm_idle_seconds`, in ms: an entry idle this long goes. */
  idleMs: number
  now?: () => number
  log?: (message: string) => void
}

/** USD: how far the remaining budget may sit from the process's cap minus its spend and still match (§5.3). */
const BUDGET_EPSILON = 1e-6
/** The least cap a spawn is given, as TurnRunner's (`maxBudgetUsd` must be positive). */
const MIN_CAP_USD = 0.000001

/**
 * The service's transcript mirror as one process sees it: every append goes
 * through to `inner`, in call order, and the last main-transcript uuid that
 * landed is remembered. The transcript tail rule compares it with the table's
 * tail: equal unless another process wrote since. A process's own writes can
 * land after its `result` (a `prompt_snapshot` attachment and `last-prompt`
 * did; a streaming process's `cost-state` only as it ends: measured against
 * the fake endpoint, test/warm.pg.test.ts), so the rule never compares a tail
 * read at the result with a later one.
 */
class MirroredTranscript {
  readonly store: SessionStore
  lastUuid: string | null
  /** Every append so far, in order; never rejects. */
  pending: Promise<void> = Promise.resolve()

  constructor(inner: SessionStore, tail: string | null) {
    this.lastUuid = tail
    const append = (key: SessionKey, entries: SessionStoreEntry[]): Promise<void> => {
      const write = this.pending.then(() => inner.append(key, entries))
      this.pending = write.then(
        () => {
          if ((key.subpath ?? '') !== '') return
          const uuid = entries.findLast((e) => typeof e.uuid === 'string')?.uuid
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
  credential: Credential
  mirror: MirroredTranscript | undefined
  /** What the transcript's last `cost-state` held at spawn: what a resume restores (manager.ts `finish`). */
  carriedUsd: number
  settled: boolean
  /** Between `acquire` and `settle`: never evicted for idleness or the cap. */
  busy: boolean
  /** Evicted while busy: closed at `settle`, or recycled by the next `acquire`. */
  doomed?: string
  timer?: ReturnType<typeof setTimeout>
  closed: boolean
}

const sameCredential = (a: Credential, b: Credential) =>
  a.kind === b.kind && a.secret === b.secret && (a.kind === 'gateway' ? a.baseUrl : '') === (b.kind === 'gateway' ? b.baseUrl : '')

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
  private readonly now: () => number
  /** By session, least recently used first. */
  private readonly slots = new Map<string, Slot>()
  /** Each session's acquire, one at a time: two would spawn two processes. */
  private readonly acquiring = new Map<string, Promise<unknown>>()

  constructor(deps: WarmSessionsDeps) {
    this.deps = deps
    this.now = deps.now ?? Date.now
  }

  /**
   * The session's process for this turn, which the turn then holds until
   * `settle`. On a throw the assembly is still the caller's to release.
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
      this.drop(slot, slot.doomed)
      return
    }
    this.arm(slot)
    this.trim()
  }

  /** Closes the session's process (`*`: every one). One with a turn running is closed when the turn settles. */
  evict(sessionId: string | '*', reason: string): void {
    for (const slot of [...this.slots.values()]) {
      if (sessionId !== '*' && slot.entry.sessionId !== sessionId) continue
      if (slot.busy) slot.doomed = reason
      else this.drop(slot, reason)
    }
  }

  size(): number {
    return this.slots.size
  }

  /** Closes every process, running turns' too (shutdown). */
  async close(): Promise<void> {
    const slots = [...this.slots.values()]
    for (const slot of slots) this.drop(slot, 'shutdown')
    await Promise.all(slots.map((slot) => slot.mirror?.pending))
  }

  private async acquireNow(sessionId: string, turn: AssembledTurn, credential: Credential, remainingUsd: number): Promise<WarmTurn> {
    this.sweepIdle()
    const print = fingerprint(turn.fingerprintParts)
    const slot = this.slots.get(sessionId)
    let reason: RecycleReason | undefined
    if (slot) {
      reason = await this.mismatch(slot, print, credential, remainingUsd)
      if (reason === undefined) {
        turn.release()
        clearTimeout(slot.timer)
        slot.busy = true
        slot.entry.lastUsedAt = this.now()
        this.slots.delete(sessionId)
        this.slots.set(sessionId, slot)
        return { entry: slot.entry, outcome: 'hit' }
      }
      this.drop(slot, `recycled: ${reason}`)
      // Its last appends land before the new process loads the transcript.
      await slot.mirror?.pending
    }
    const entry = await this.spawn(sessionId, turn, credential, remainingUsd, print)
    this.trim()
    return reason === undefined ? { entry, outcome: 'miss' } : { entry, outcome: 'recycle', reason }
  }

  /** Why the process cannot serve this turn (§5.3 "Reuse rule"), or undefined when it can. */
  private async mismatch(slot: Slot, print: string, credential: Credential, remainingUsd: number): Promise<RecycleReason | undefined> {
    // Still held: evicted during its turn, or its turn ended without `settle`,
    // so its stream may yet hold that turn's messages.
    if (slot.busy || slot.doomed !== undefined) return 'evicted'
    // The credential is in the process's env (run.ts `credentialEnv`); a
    // fallback to another candidate keeps the fingerprint but not the key.
    if (slot.entry.fingerprint !== print || !sameCredential(slot.credential, credential)) return 'fingerprint'
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
      if (mirror.pending === pending) return tail !== mirror.lastUuid
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
    const [row] = await this.deps.sql<{ tail: string | null; resume: boolean; cost_state: string | null }[]>`
      SELECT
        (SELECT uuid FROM ai_session_entries
          WHERE session_id = ${sessionId} AND subpath = '' AND uuid IS NOT NULL
          ORDER BY id DESC LIMIT 1) AS tail,
        EXISTS (SELECT 1 FROM ai_session_entries WHERE session_id = ${sessionId} AND subpath = '') AS resume,
        (SELECT entry FROM ai_session_entries
          WHERE session_id = ${sessionId} AND subpath = '' AND uuid IS NULL AND entry LIKE '%"cost-state"%'
          ORDER BY id DESC LIMIT 1) AS cost_state`
    const carriedUsd = row?.resume ? costStateUsd(row.cost_state) : 0
    const mirror = turn.run.sessionStore ? new MirroredTranscript(turn.run.sessionStore, row?.tail ?? null) : undefined
    const inbox = new Inbox()
    const { resume: _resume, sessionId: _sessionId, ...run } = turn.run
    const query = runHarness({
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
    this.slots.set(sessionId, { entry, credential, mirror, carriedUsd, settled: false, busy: true, closed: false })
    return entry
  }

  /** The idle timer (unref'd): it goes once `now` says the entry has been idle for `idleMs`. */
  private arm(slot: Slot): void {
    clearTimeout(slot.timer)
    const check = () => {
      if (slot.closed || slot.busy) return
      const idle = this.now() - slot.entry.lastUsedAt
      if (idle >= this.deps.idleMs) this.drop(slot, 'idle')
      else schedule(this.deps.idleMs - idle)
    }
    const schedule = (ms: number) => {
      slot.timer = setTimeout(check, ms)
      slot.timer.unref()
    }
    schedule(this.deps.idleMs)
  }

  private sweepIdle(): void {
    for (const slot of [...this.slots.values()]) {
      if (!slot.busy && this.now() - slot.entry.lastUsedAt >= this.deps.idleMs) this.drop(slot, 'idle')
    }
  }

  /** Past `maxEntries`, the least recently used idle entries go; never one with a turn running. */
  private trim(): void {
    while (this.slots.size > this.deps.maxEntries) {
      let lru: Slot | undefined
      for (const slot of this.slots.values()) {
        if (!slot.busy && (!lru || slot.entry.lastUsedAt < lru.entry.lastUsedAt)) lru = slot
      }
      if (!lru) return
      this.drop(lru, 'lru')
    }
  }

  /** Closes the process (its stdin, then the query), then releases the assembly it was spawned with. */
  private drop(slot: Slot, reason: string): void {
    if (slot.closed) return
    slot.closed = true
    clearTimeout(slot.timer)
    const id = slot.entry.sessionId
    if (this.slots.get(id) === slot) this.slots.delete(id)
    slot.entry.inbox.close()
    slot.entry.query.close()
    slot.entry.turn.release()
    this.deps.log?.(`warm: closed session ${id}'s process: ${reason}`)
  }
}

/** The `totalCostUSD` of a `cost-state` transcript entry; 0 when there is none. */
function costStateUsd(entry: string | null): number {
  if (entry === null) return 0
  const parsed = JSON.parse(entry) as { type?: unknown; totalCostUSD?: unknown }
  return parsed.type === 'cost-state' && typeof parsed.totalCostUSD === 'number' ? parsed.totalCostUSD : 0
}
