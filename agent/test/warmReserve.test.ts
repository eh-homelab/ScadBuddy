import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'
import { describe, expect, it, vi } from 'vitest'
import type { HarnessRun } from '../src/harness/run.js'
import type { AssembledTurn } from '../src/sessions/turnRunner.js'
import { TurnHost, type TurnHostDeps } from '../src/sessions/turnHost.js'
import { createTurnSlot } from '../src/sessions/turnSlot.js'
import { RESERVATION_HOLD_MS, type WarmQuery, WarmSessions } from '../src/sessions/warm.js'
import { acquireActivities, releaseActivities } from '../src/temporal/session/affinity.js'
import { ACQUIRE_WORKER_ACTIVITY, NO_CAPACITY, RELEASE_WORKER_ACTIVITY, SHUTTING_DOWN_FAILURE } from '../src/temporal/session/names.js'

// WarmSessions' places for AgentSession (spec 2026-10-13 §5.2): `reserve` is acquire_worker's,
// `release` release_worker's. A full replica gives up its least recently used idle process or
// reservation, never one running a turn, and refuses only when every one runs a turn.

const credential = { kind: 'anthropic_api_key' as const, secret: 'sk-ant-test' }
/** acquire_worker's listing of the live session hosts, where the test has one replica and no turn was lost. */
const alone = () => Promise.resolve(new Set(['agent-sessions.r']))

function assembly(): AssembledTurn {
  return {
    run: { paths: { stateDir: '/nonexistent' } },
    candidates: [],
    fingerprintParts: {
      credentialId: 'c1',
      credentialUpdatedAt: '1',
      model: null,
      effort: null,
      agentType: null,
      ownPlugin: null,
      plugins: [],
      packageHashes: [],
      allowedPackageHashes: [],
      tiers: null,
      owner: 'user:alice',
      systemAppend: '',
      disabledTools: [],
      raisedTiers: [],
      httpRequest: false,
      toolsAsActivities: false,
      httpTier: null,
      builtinTools: [],
      servedBuiltins: [],
      backgroundTasks: false,
      recallMaxQueryChars: null,
      transientRetries: null,
    },
    slot: createTurnSlot(),
    secrets: [],
    eventTierOf: () => undefined,
    shownTierOf: () => undefined,
    observe: () => Promise.resolve(),
    terminate: vi.fn(async () => {}),
    release: vi.fn(),
  }
}

/** A process that ends when closed; nothing is ever pushed in these tests. */
function process(): WarmQuery & { closed: () => boolean } {
  let closed = false
  let wake: (() => void) | undefined
  return {
    closed: () => closed,
    [Symbol.asyncIterator]: () => ({
      next: () =>
        closed ? Promise.resolve({ done: true, value: undefined }) : new Promise<IteratorResult<SDKMessage>>((resolve) => {
          wake = () => resolve({ done: true, value: undefined })
        }),
    }),
    interrupt: () => Promise.resolve(undefined),
    close: () => {
      closed = true
      wake?.()
    },
  }
}

function cache(maxEntries: number) {
  let clock = 1_000
  const spawned = new Map<string, ReturnType<typeof process>>()
  // A session with no transcript: spawn and the tail read find nothing.
  const sql = (() => Promise.resolve([{ tail: null, resume: false }])) as unknown as Sql
  const warm = new WarmSessions({
    sql,
    store: { lastOfType: () => Promise.resolve(null) },
    maxEntries,
    idleMs: 600_000,
    now: () => clock,
    start: (run: HarnessRun) => {
      const p = process()
      spawned.set(run.sessionId ?? run.resume ?? '', p)
      return p
    },
  })
  /** A process for `id`: acquired, so running a turn until `left`. */
  const running = async (id: string) => {
    clock += 10
    const { entry } = await warm.acquire(id, assembly(), credential, 1)
    return { entry, left: () => warm.leave(entry) }
  }
  return { warm, spawned, running, tick: (ms = 10) => (clock += ms) }
}

describe('WarmSessions.reserve and release', () => {
  it('reserves a place, again for the same session, and counts it against the cap', async () => {
    const { warm, tick } = cache(2)
    expect(warm.reserve('a')).toBe('reserved')
    expect(warm.reserve('a')).toBe('reserved')
    tick()
    expect(warm.reserve('b')).toBe('reserved')
    // Full of reservations still being placed (their turns on the way): a third is refused.
    tick()
    expect(warm.reserve('c')).toBe('full')
    // Past the hold, the oldest one's place is given up for it.
    tick(RESERVATION_HOLD_MS)
    expect(warm.reserve('c')).toBe('reserved')
    expect(warm.reserve('b')).toBe('reserved')
  })

  it('warm_max_per_replica 0 admits every session: its turns run cold', async () => {
    const { warm, spawned, running } = cache(0)
    expect(warm.reserve('a')).toBe('reserved')
    const acquire = acquireActivities({ warm, hostQueue: 'agent-sessions.r', identity: 'r', liveHosts: alone })[ACQUIRE_WORKER_ACTIVITY]
    await expect(acquire({ sessionId: 'b' })).resolves.toEqual({ hostQueue: 'agent-sessions.r', identity: 'r' })
    // The turn's process is spawned, and closed once the turn leaves it.
    const turn = await running('a')
    turn.left()
    await vi.waitFor(() => expect(spawned.get('a')?.closed()).toBe(true), { timeout: 5_000 })
    expect(warm.size()).toBe(0)
  })

  it('a session whose turn was lost here (exclude) is referred to the other live replicas, and taken when there are none', async () => {
    const { warm } = cache(8)
    const reserved: string[] = []
    const reserving = { reserve: (id: string) => (reserved.push(id), warm.reserve(id)) }
    const acquire = (live: string[]) => acquireActivities({ warm: reserving, hostQueue: 'agent-sessions.r', identity: 'r', liveHosts: () => Promise.resolve(new Set(live)) })[ACQUIRE_WORKER_ACTIVITY]
    const here = { hostQueue: 'agent-sessions.r', identity: 'r' }
    // Another replica polls: a referral there, and no place reserved here for a turn that is leaving.
    await expect(acquire(['agent-sessions.r', 'agent-sessions.other'])({ sessionId: 'a', exclude: ['agent-sessions.r'] })).resolves.toEqual({ ...here, elsewhere: ['agent-sessions.other'] })
    expect(reserved).toEqual([])
    // A home the turn was lost on elsewhere does not count against this one.
    await expect(acquire(['agent-sessions.r', 'agent-sessions.other'])({ sessionId: 'a', exclude: ['agent-sessions.other'] })).resolves.toEqual(here)
    // A preference, not a ban: with no other replica (a restart in place keeps the queue's name;
    // a lone replica whose heartbeat stalled), or only ones the turn was lost on too, this one takes it.
    await expect(acquire(['agent-sessions.r'])({ sessionId: 'b', exclude: ['agent-sessions.r'] })).resolves.toEqual(here)
    await expect(acquire([])({ sessionId: 'c', exclude: ['agent-sessions.r'] })).resolves.toEqual(here)
    await expect(acquire(['agent-sessions.r', 'agent-sessions.other'])({ sessionId: 'd', exclude: ['agent-sessions.r', 'agent-sessions.other'] })).resolves.toEqual(here)
    expect(reserved).toEqual(['a', 'b', 'c', 'd'])
    // Who polls is unknown (Temporal did not answer, or the role may not describe a queue): no
    // list, so this one takes the turn as if it were alone, never failing the attempt.
    const unknown = acquireActivities({ warm: reserving, hostQueue: 'agent-sessions.r', identity: 'r', liveHosts: () => Promise.reject(new Error('describe failed')) })[ACQUIRE_WORKER_ACTIVITY]
    await expect(unknown({ sessionId: 'e', exclude: ['agent-sessions.r'] })).resolves.toEqual(here)
    expect(reserved).toEqual(['a', 'b', 'c', 'd', 'e'])
    // Not excluded: nobody is asked who polls.
    await expect(unknown({ sessionId: 'f' })).resolves.toEqual(here)
  })

  it('a referral lists the other replicas in an order of its own each time, so referred turns spread', async () => {
    const { warm } = cache(8)
    const live = ['agent-sessions.r', 'agent-sessions.x', 'agent-sessions.y', 'agent-sessions.z']
    const acquire = acquireActivities({ warm, hostQueue: 'agent-sessions.r', identity: 'r', liveHosts: () => Promise.resolve(new Set(live)) })[ACQUIRE_WORKER_ACTIVITY]
    const random = vi.spyOn(Math, 'random')
    try {
      // Fisher-Yates with every draw 0: the listing [x, y, z] comes out [y, z, x].
      random.mockReturnValue(0)
      await expect(acquire({ sessionId: 'a', exclude: ['agent-sessions.r'] })).resolves.toMatchObject({ elsewhere: ['agent-sessions.y', 'agent-sessions.z', 'agent-sessions.x'] })
      // Every draw just under 1: each item stays where it is.
      random.mockReturnValue(0.999)
      await expect(acquire({ sessionId: 'a', exclude: ['agent-sessions.r'] })).resolves.toMatchObject({ elsewhere: ['agent-sessions.x', 'agent-sessions.y', 'agent-sessions.z'] })
    } finally {
      random.mockRestore()
    }
  })

  it('cut closes a placement`s process at once, busy or not, and frees its place', async () => {
    const { warm, spawned, running } = cache(1)
    const cutOff = await running('a')
    expect(warm.busy('a')).toBe(true)
    await warm.cut(cutOff.entry, 'moved')
    expect(spawned.get('a')?.closed()).toBe(true)
    expect(warm.busy('a')).toBe(false)
    expect(warm.size()).toBe(0)
    // The turn's own end afterwards finds nothing to give back.
    cutOff.left()
    expect(warm.size()).toBe(0)
  })

  it('a full replica evicts its least recently used idle process, never one running a turn', async () => {
    const { warm, spawned, running } = cache(2)
    const busy = await running('busy')
    const idle = await running('idle')
    idle.left()
    expect(warm.size()).toBe(2)
    expect(warm.reserve('new')).toBe('reserved')
    await vi.waitFor(() => expect(spawned.get('idle')?.closed()).toBe(true), { timeout: 5_000 })
    expect(spawned.get('busy')?.closed()).toBe(false)
    expect(warm.size()).toBe(1)
    busy.left()
  })

  it('refuses only when every process here runs a turn', async () => {
    const { warm, spawned, running } = cache(2)
    const one = await running('one')
    const two = await running('two')
    expect(warm.reserve('three')).toBe('full')
    expect(spawned.get('one')?.closed()).toBe(false)
    expect(spawned.get('two')?.closed()).toBe(false)
    // As acquire_worker says it: retryable NoCapacity, so another replica may answer.
    const acquire = acquireActivities({ warm, hostQueue: 'agent-sessions.r', identity: 'r', liveHosts: alone })[ACQUIRE_WORKER_ACTIVITY]
    await expect(acquire({ sessionId: 'three' })).rejects.toMatchObject({ type: NO_CAPACITY, nonRetryable: false })
    two.left()
    await expect(acquire({ sessionId: 'three' })).resolves.toEqual({ hostQueue: 'agent-sessions.r', identity: 'r' })
    one.left()
  })

  it('a kept home whose reservation lapsed re-reserves as its turn arrives: full of running turns, session_turn refuses NoCapacity and spawns nothing past the cap (#3443)', async () => {
    const { warm, spawned, running, tick } = cache(2)
    expect(warm.reserve('kept')).toBe('reserved')
    // The session's place lapses (`warm_idle_seconds`) while the workflow keeps it as home, and
    // other sessions' turns fill the cache.
    tick(600_000)
    const one = await running('one')
    const two = await running('two')
    // Nothing of the turn is read or written before the refusal: no database is reached.
    const sql = (() => Promise.reject(new Error('the refused turn touched the database'))) as unknown as Sql
    const host = new TurnHost({ core: { warm } as unknown as TurnHostDeps['core'], sql, images: { get: vi.fn(), putIf: vi.fn() }, hostQueue: 'agent-sessions.r' })
    const control = { heartbeat: () => {}, cancelled: new AbortController().signal, cancelReason: () => 'interrupt' as const }
    await expect(host.run({ sessionId: 'kept', turnId: 't1', attempt: 'run:0' }, control)).rejects.toMatchObject({ type: NO_CAPACITY })
    expect(spawned.has('kept')).toBe(false)
    expect(warm.size()).toBe(2)
    expect(host.active).toBe(0)
    // A slot that is idle again is given up for it: the next arrival takes its place.
    two.left()
    expect(warm.reserve('kept')).toBe('reserved')
    await vi.waitFor(() => expect(spawned.get('two')?.closed()).toBe(true), { timeout: 5_000 })
    expect(spawned.get('one')?.closed()).toBe(false)
    one.left()
  })

  it('a placement that fails before its spawn gives back the place it reserved on arrival', async () => {
    const { warm, spawned } = cache(1)
    // The claim's read fails: the turn ends before anything is spawned.
    const sql = (() => Promise.reject(new Error('the database is gone'))) as unknown as Sql
    const host = new TurnHost({ core: { warm } as unknown as TurnHostDeps['core'], sql, images: { get: vi.fn(), putIf: vi.fn() }, hostQueue: 'agent-sessions.r' })
    const control = { heartbeat: () => {}, cancelled: new AbortController().signal, cancelReason: () => 'interrupt' as const }
    await expect(host.run({ sessionId: 'gone', turnId: 't1', attempt: 'run:0' }, control)).rejects.toThrow('the database is gone')
    expect(spawned.has('gone')).toBe(false)
    // Held for RESERVATION_HOLD_MS had it been kept: the one place is free at once.
    expect(warm.reserve('next')).toBe('reserved')
  })

  it('a placement cut by a newer one of the same session never gives back the newer one`s place', async () => {
    const { warm } = cache(1)
    // Each claim waits until the test fails it: the older placement is still claiming when the newer arrives.
    const claims: ((err: Error) => void)[] = []
    const sql = (() => new Promise((_resolve, reject) => claims.push(reject))) as unknown as Sql
    const host = new TurnHost({ core: { warm } as unknown as TurnHostDeps['core'], sql, images: { get: vi.fn(), putIf: vi.fn() }, hostQueue: 'agent-sessions.r' })
    const control = () => ({ heartbeat: () => {}, cancelled: new AbortController().signal, cancelReason: () => 'interrupt' as const })
    const older = host.run({ sessionId: 's', turnId: 't1', attempt: 'run:0' }, control())
    await vi.waitFor(() => expect(claims).toHaveLength(1))
    // The newer arrives (the same attempt id: the next turn's first placement in a run shares it) and cuts the older.
    const newer = host.run({ sessionId: 's', turnId: 't2', attempt: 'run:0' }, control())
    claims[0]!(new Error('the older claim failed'))
    await expect(older).rejects.toThrow('the older claim failed')
    await vi.waitFor(() => expect(claims).toHaveLength(2))
    // The one place is still the newer placement's.
    expect(warm.reserve('other')).toBe('full')
    claims[1]!(new Error('the newer claim failed'))
    await expect(newer).rejects.toThrow('the newer claim failed')
    expect(warm.reserve('other')).toBe('reserved')
  })

  it('a draining replica refuses an arriving placement ShuttingDown, before it reserves or evicts anything', async () => {
    const { warm, spawned, running } = cache(1)
    const idle = await running('idle')
    idle.left()
    const sql = (() => Promise.reject(new Error('the refused turn touched the database'))) as unknown as Sql
    const host = new TurnHost({ core: { warm } as unknown as TurnHostDeps['core'], sql, images: { get: vi.fn(), putIf: vi.fn() }, hostQueue: 'agent-sessions.r' })
    host.drain()
    const control = { heartbeat: () => {}, cancelled: new AbortController().signal, cancelReason: () => 'interrupt' as const }
    await expect(host.run({ sessionId: 'late', turnId: 't1', attempt: 'run:0' }, control)).rejects.toMatchObject({ type: SHUTTING_DOWN_FAILURE })
    expect(spawned.get('idle')?.closed()).toBe(false)
    expect(warm.size()).toBe(1)
  })

  it('unreserve gives back only a reservation: a session`s process stays', async () => {
    const { warm, spawned, running } = cache(1)
    const turn = await running('a')
    warm.unreserve('a')
    turn.left()
    warm.unreserve('a')
    expect(spawned.get('a')?.closed()).toBe(false)
    expect(warm.size()).toBe(1)
    // Still its place: another session takes it only by evicting the idle process.
    expect(warm.reserve('a')).toBe('reserved')
  })

  it('a session with a process here keeps it, without a second place', async () => {
    const { warm, running } = cache(1)
    const own = await running('own')
    expect(warm.reserve('own')).toBe('reserved')
    own.left()
  })

  it('release frees the place and closes the idle process; a running one goes when its turn leaves it', async () => {
    const { warm, spawned, running } = cache(2)
    const release = releaseActivities(warm)[RELEASE_WORKER_ACTIVITY]
    const idle = await running('idle')
    idle.left()
    await release({ sessionId: 'idle' })
    expect(warm.size()).toBe(0)
    expect(spawned.get('idle')?.closed()).toBe(true)
    const busy = await running('busy')
    await release({ sessionId: 'busy' })
    expect(spawned.get('busy')?.closed()).toBe(false)
    busy.left()
    await vi.waitFor(() => expect(spawned.get('busy')?.closed()).toBe(true), { timeout: 5_000 })
    expect(warm.size()).toBe(0)
    // A reservation alone is freed too: the cap has room for two again.
    expect(warm.reserve('x')).toBe('reserved')
    await release({ sessionId: 'x' })
    expect(warm.reserve('y')).toBe('reserved')
    expect(warm.reserve('z')).toBe('reserved')
  })
})
