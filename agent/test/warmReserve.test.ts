import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'
import { describe, expect, it, vi } from 'vitest'
import type { HarnessRun } from '../src/harness/run.js'
import type { AssembledTurn } from '../src/sessions/turnRunner.js'
import { createTurnSlot } from '../src/sessions/turnSlot.js'
import { type WarmQuery, WarmSessions } from '../src/sessions/warm.js'
import { acquireActivities, releaseActivities } from '../src/temporal/session/affinity.js'
import { ACQUIRE_WORKER_ACTIVITY, NO_CAPACITY, RELEASE_WORKER_ACTIVITY } from '../src/temporal/session/names.js'

// WarmSessions' places for AgentSession (spec 2026-10-13 §5.2): `reserve` is acquire_worker's,
// `release` release_worker's. A full replica gives up its least recently used idle process or
// reservation, never one running a turn, and refuses only when every one runs a turn.

const credential = { kind: 'anthropic_api_key' as const, secret: 'sk-ant-test' }

function assembly(): AssembledTurn {
  return {
    run: { paths: { stateDir: '/nonexistent' } },
    candidates: [],
    fingerprintParts: {
      credentialId: 'c1',
      credentialUpdatedAt: '1',
      model: null,
      ownPlugin: false,
      plugins: [],
      packageHashes: [],
      allowedPackageHashes: [],
      tiers: null,
      owner: 'user:alice',
      systemAppend: '',
      disabledTools: [],
      raisedTiers: [],
      httpRequest: false,
      builtinTools: [],
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
  return { warm, spawned, running, tick: () => (clock += 10) }
}

describe('WarmSessions.reserve and release', () => {
  it('reserves a place, again for the same session, and counts it against the cap', async () => {
    const { warm, tick } = cache(2)
    expect(warm.reserve('a')).toBe('reserved')
    expect(warm.reserve('a')).toBe('reserved')
    tick()
    expect(warm.reserve('b')).toBe('reserved')
    // Full of reservations: a third takes the oldest one's place.
    tick()
    expect(warm.reserve('c')).toBe('reserved')
    expect(warm.reserve('a')).toBe('reserved')
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
    const acquire = acquireActivities({ warm, hostQueue: 'agent-sessions.r', identity: 'r' })[ACQUIRE_WORKER_ACTIVITY]
    await expect(acquire({ sessionId: 'three' })).rejects.toMatchObject({ type: NO_CAPACITY, nonRetryable: false })
    two.left()
    await expect(acquire({ sessionId: 'three' })).resolves.toEqual({ hostQueue: 'agent-sessions.r', identity: 'r' })
    one.left()
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
