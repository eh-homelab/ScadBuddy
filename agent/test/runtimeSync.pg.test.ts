import { randomBytes, randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CredentialStore, SettingsStore } from '../src/credentials.js'
import { CredentialPool } from '../src/harness/fallback.js'
import { kekFromBase64 } from '../src/secrets.js'
import type { Database } from '../src/db.js'
import { PgEventListener } from '../src/events/pgListener.js'
import { userMessage } from '../src/harness/inbox.js'
import { PostgresSessionStore } from '../src/sessions/store.js'
import type { AssembledTurn } from '../src/sessions/turnRunner.js'
import { createTurnSlot } from '../src/sessions/turnSlot.js'
import { WarmSessions } from '../src/sessions/warm.js'
import { readStartSettings, wireRuntimeSync } from '../src/sessions/runtimeSync.js'
import { RUNTIME_DEFAULTS, RUNTIME_PAYLOAD, SETTINGS_CHANNEL } from '../src/sessions/runtimeSettings.js'
import { WARM_EVICT_CHANNEL } from '../src/sessions/warmEvict.js'
import { agentA, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { nextResult } from './support/warm.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { fixedCredentials } from './support/fixedCredentials.js'
import { AGENT_SESSIONS_SKIP, agentSessions } from './support/agentSessions.js'

// How the warm cache follows the settings and evictions when a NOTIFY never
// arrives (sessions/runtimeSync.ts): the start read, the reconnect and the resync.

const credential = { kind: 'anthropic_api_key' as const, secret: 'sk-ant-test' }

describe('readStartSettings', () => {
  const settings = { get: <T>(key: string) => Promise.resolve((key === 'warm_max_per_replica' ? 3 : undefined) as T) }

  it('reads the stored settings once the database is ready', async () => {
    const log = vi.fn()
    expect(await readStartSettings({ database: { ready: () => Promise.resolve(true) }, settings }, log)).toEqual({ ...RUNTIME_DEFAULTS, warm_max_per_replica: 3 })
    expect(log).not.toHaveBeenCalled()
  })

  it('has the defaults, silently, with no database', async () => {
    const log = vi.fn()
    expect(await readStartSettings({ database: undefined, settings: undefined }, log)).toEqual(RUNTIME_DEFAULTS)
    expect(log).not.toHaveBeenCalled()
  })

  it('logs the defaults it falls back to when the database is not ready, or the read throws', async () => {
    const log = vi.fn()
    expect(await readStartSettings({ database: { ready: () => Promise.resolve(false) }, settings }, log)).toEqual(RUNTIME_DEFAULTS)
    expect(log).toHaveBeenLastCalledWith(expect.stringContaining('not ready'))
    const broken = { get: () => Promise.reject(new Error('no table')) }
    expect(await readStartSettings({ database: { ready: () => Promise.resolve(true) }, settings: broken }, log)).toEqual(RUNTIME_DEFAULTS)
    expect(log).toHaveBeenLastCalledWith(expect.stringContaining('no table'))
    expect(log).toHaveBeenCalledTimes(2)
  })
})

describe.skipIf((!TEST_DATABASE_URL) || AGENT_SESSIONS_SKIP !== '')(`the warm cache's resync${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  agentSessions()
  let db: Database
  let drop: () => Promise<void>
  let url: string
  let schema: string
  let warm: WarmSessions
  let store: PostgresSessionStore
  let scripted: ReturnType<typeof scriptedRunner>
  let stops: (() => void | Promise<void>)[]

  beforeEach(async () => {
    ;({ db, drop, url, schema } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new PostgresSessionStore(db.sql, { keys: testPayloadKeys(db.sql) })
    scripted = scriptedRunner(() => ({ reply: 'ok', costUsd: 0.02 }))
    warm = new WarmSessions({ sql: db.sql, store, maxEntries: 8, idleMs: 600_000, start: scripted.runner })
    stops = []
  })
  afterEach(async () => {
    for (const stop of stops.splice(0).reverse()) await stop()
    await warm.close()
    await drop()
  })

  async function assembled(
    credentialId = 'c1',
    candidates: AssembledTurn['candidates'] = [{ id: credentialId, epoch: 0, label: 'test', credential }],
  ): Promise<AssembledTurn> {
    return {
      run: { paths: await tempPaths(), sessionStore: store, maxTurns: 5 },
      candidates,
      fingerprintParts: {
        credentialId,
        credentialUpdatedAt: '1',
        model: null,
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
        httpTier: null,
        builtinTools: [],
        recallMaxQueryChars: null,
        transientRetries: null,
        servedBuiltins: [],
        backgroundTasks: false,
        effort: null,
        agentType: null,
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

  async function turn(id: string) {
    const acquired = await warm.acquire(id, await assembled(), credential, 1)
    acquired.entry.inbox.push(userMessage('hello'))
    await warm.settle(acquired.entry, await nextResult(acquired.entry.iterator))
    return acquired
  }

  /** A listener standing in for the event bus's, which a test can silence and bring back. */
  function fakeEvents() {
    const heard = new Map<string, (payload: string) => void>()
    const reconnects: (() => void)[] = []
    let down = false
    return {
      events: {
        listenAlso: (channel: string, fn: (payload: string) => void): void => {
          heard.set(channel, fn)
        },
        onReconnect: (fn: () => void): void => {
          reconnects.push(fn)
        },
      },
      /** A NOTIFY while the listener is up reaches its consumer; while it is down it is lost. */
      notify: (channel: string, payload: string) => {
        if (!down) heard.get(channel)?.(payload)
      },
      drop: () => void (down = true),
      reconnect: () => {
        down = false
        for (const fn of reconnects) fn()
      },
    }
  }

  const sync = (events: ReturnType<typeof fakeEvents>['events'], extra: { revalidate?: () => Promise<void>; resyncMs?: number } = {}) => {
    const stop = wireRuntimeSync({
      settings: new SettingsStore(db.sql),
      warm,
      revalidate: extra.revalidate ?? (() => Promise.resolve()),
      events,
      log: () => {},
      resyncMs: extra.resyncMs ?? 3_600_000,
    })
    stops.push(stop)
  }

  it('after a reconnect, evicts every idle process, re-reads the settings and resizes; a running turn is left alone', async () => {
    const fake = fakeEvents()
    sync(fake.events)
    const [a, b, c] = [randomUUID(), randomUUID(), randomUUID()]
    await turn(a)
    await turn(b)
    const running = await warm.acquire(c, await assembled(), credential, 1)
    running.entry.inbox.push(userMessage('hello'))
    expect(warm.size()).toBe(3)

    // While the listener is down: a setting changes and a credential is revoked, and no NOTIFY is heard.
    fake.drop()
    await new SettingsStore(db.sql).setMany({ warm_max_per_replica: 1 })
    fake.notify('scadbuddy_settings', 'session-runtime')
    fake.notify(WARM_EVICT_CHANNEL, '*')
    expect(warm.size()).toBe(3)

    fake.reconnect()
    await vi.waitFor(() => expect(warm.size()).toBe(1))
    expect(running.entry.turn.release).not.toHaveBeenCalled()
    await warm.settle(running.entry, await nextResult(running.entry.iterator))
    // The new size holds: the settled process stays (one allowed), a second one goes.
    expect(warm.size()).toBe(1)
    await turn(a)
    await vi.waitFor(() => expect(warm.size()).toBe(1))
  })

  it('a reconnect evicts every idle process even when no setting changed, and leaves a running turn alone', async () => {
    await db.sql.unsafe(`CREATE TABLE events (
      seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, event_id text NOT NULL UNIQUE, kind text NOT NULL,
      at timestamptz NOT NULL, logged_at timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL)`)
    const listener = new PgEventListener(url, { searchPath: schema, retryMinMs: 20, retryMaxMs: 100, log: () => {} })
    stops.push(() => listener.close())
    sync(listener)
    listener.start()
    await listener.ready()
    const [a, b, c] = [randomUUID(), randomUUID(), randomUUID()]
    await turn(a)
    await turn(b)
    const running = await warm.acquire(c, await assembled(), credential, 1)
    running.entry.inbox.push(userMessage('hello'))
    expect(warm.size()).toBe(3)

    // Nothing stored changed (the max stays 8): only the eviction a missed NOTIFY could have carried.
    await listener.dropConnectionForTest()
    await vi.waitFor(() => expect(warm.size()).toBe(1), { timeout: 5000 })
    expect(running.entry.turn.release).not.toHaveBeenCalled()
    const kept = await warm.acquire(c, await assembled(), credential, 1).catch((err: unknown) => err)
    expect(kept).toMatchObject({ name: 'WarmBusyError' })
    await warm.settle(running.entry, await nextResult(running.entry.iterator))
    expect(warm.size()).toBe(1)
  })

  it('a real listener that dropped and came back runs the same recovery', async () => {
    await db.sql.unsafe(`CREATE TABLE events (
      seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, event_id text NOT NULL UNIQUE, kind text NOT NULL,
      at timestamptz NOT NULL, logged_at timestamptz NOT NULL DEFAULT now(), payload jsonb NOT NULL)`)
    const listener = new PgEventListener(url, { searchPath: schema, retryMinMs: 20, retryMaxMs: 100, log: () => {} })
    stops.push(() => listener.close())
    sync(listener)
    listener.start()
    await listener.ready()
    const id = randomUUID()
    await turn(id)
    // Written without a NOTIFY: what the settings write looks like to a listener that was down.
    await db.sql`INSERT INTO ai_settings (key, value) VALUES ('warm_max_per_replica', '0')`
    await listener.dropConnectionForTest()
    await vi.waitFor(() => expect(warm.size()).toBe(0), { timeout: 5000 })
    // Size 0 applied: the next turn's process goes when it settles.
    await turn(id)
    await vi.waitFor(() => expect(warm.size()).toBe(0))
  })

  it('the resync drops an idle process whose credential changed with no NOTIFY delivered, and keeps one that still matches', async () => {
    const [stale, fresh] = [randomUUID(), randomUUID()]
    await turn(stale)
    await turn(fresh)
    expect(warm.size()).toBe(2)
    // The credential `stale` was spawned with is revoked; `fresh`'s still stands.
    const assemble = async (entry: { sessionId: string }) => assembled(entry.sessionId === stale ? 'c2' : 'c1')
    sync(fakeEvents().events, { revalidate: () => warm.revalidate(assemble), resyncMs: 40 })
    await vi.waitFor(() => expect(warm.size()).toBe(1), { timeout: 5000 })
    const kept = await warm.acquire(fresh, await assembled(), credential, 1 - 0.02)
    expect(kept.outcome).toBe('hit')
    warm.leave(kept.entry)
  })

  it('checks an idle process against the credential it was spawned on: a healthy one on a fallback stays, a removed or changed one goes', async () => {
    const primary = { id: 'c0', epoch: 0, label: 'first', credential: { kind: 'anthropic_api_key' as const, secret: 'sk-ant-first' } }
    const fallback = { id: 'c1', epoch: 0, label: 'second', credential: { kind: 'anthropic_api_key' as const, secret: 'sk-ant-second' } }
    const spawn = async (id: string) => {
      // The turn fell back to the second candidate; its fingerprint is the first's (turnRunner.ts).
      const acquired = await warm.acquire(id, await assembled('c0', [primary, fallback]), fallback.credential, 1)
      acquired.entry.inbox.push(userMessage('hello'))
      await warm.settle(acquired.entry, await nextResult(acquired.entry.iterator))
    }
    const [healthy, changed, removed] = [randomUUID(), randomUUID(), randomUUID()]
    for (const id of [healthy, changed, removed]) await spawn(id)
    expect(warm.size()).toBe(3)
    const rotated = { ...fallback, epoch: 1 }
    await warm.revalidate(async (entry) =>
      entry.sessionId === removed
        ? assembled('c0', [primary])
        : entry.sessionId === changed
          ? assembled('c0', [primary, rotated])
          : assembled('c0', [primary, fallback]),
    )
    expect(warm.size()).toBe(1)
    const kept = await warm.acquire(healthy, await assembled('c0', [primary, fallback]), fallback.credential, 1 - 0.02)
    expect(kept.outcome).toBe('hit')
    warm.leave(kept.entry)
    // The pool's first candidate changing does not touch it either.
    await warm.revalidate(() => assembled('c9', [{ ...primary, id: 'c9', epoch: 4 }, fallback]))
    expect(warm.size()).toBe(1)
  })

  it('the resync re-reads the settings, and leaves a process that is running', async () => {
    const [idle, running] = [randomUUID(), randomUUID()]
    await turn(idle)
    const held = await warm.acquire(running, await assembled(), credential, 1)
    held.entry.inbox.push(userMessage('hello'))
    await db.sql`INSERT INTO ai_settings (key, value) VALUES ('warm_max_per_replica', '1')`
    // Its credential changed too: a busy process is not revalidated.
    sync(fakeEvents().events, { revalidate: () => warm.revalidate(() => assembled('c2')), resyncMs: 40 })
    await vi.waitFor(() => expect(warm.size()).toBe(1), { timeout: 5000 })
    expect(held.entry.turn.release).not.toHaveBeenCalled()
    await warm.settle(held.entry, await nextResult(held.entry.iterator))
  })

  it('drops the process of a session that is gone, and of one that cannot be assembled', async () => {
    const [gone, broken] = [randomUUID(), randomUUID()]
    await turn(gone)
    await turn(broken)
    await warm.revalidate(async (entry) => {
      if (entry.sessionId === gone) return undefined
      throw new Error('no credential')
    })
    expect(warm.size()).toBe(0)
  })

  it('SessionManager.revalidateWarm keeps a process whose credential is cooling down, which the pool no longer lists', async () => {
    const kek = kekFromBase64(randomBytes(32).toString('base64'))
    const credentials = new CredentialStore(db.sql)
    const stored = await credentials.put({ kind: 'anthropic_api_key', secret: 'sk-ant-cooling' }, kek)
    const pool = new CredentialPool({ repo: credentials, kek: { ok: true, kek }, log: () => {} })
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, credentials: pool })
    stops.push(() => m.warm.close())
    const { turn: started } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
    await started!.done
    expect(m.warm.size()).toBe(1)
    await credentials.record(stored.id, stored.epoch, { kind: 'cooling_down', until: new Date(Date.now() + 3_600_000), reason: 'rate limited' })
    await expect(pool.candidates()).rejects.toThrow()
    await m.revalidateWarm()
    expect(m.warm.size()).toBe(1)
    // Disabled for good: the process goes.
    await credentials.record(stored.id, stored.epoch, { kind: 'disabled', reason: 'revoked' })
    await m.revalidateWarm()
    expect(m.warm.size()).toBe(0)
  })

  it("a Workflow agent's effort and type recycle the process, and SessionManager.revalidateWarm keeps the new one", async () => {
    const credentials = fixedCredentials({ kind: 'anthropic_api_key', secret: 'sk-ant-test' })
    const { runner, runs } = scriptedRunner(() => ({ reply: 'ok' }))
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, credentials })
    stops.push(() => m.warm.close())
    const { session, turn: started } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
    await started!.done
    expect(runs).toHaveLength(1)
    // Each one alone recycles: the effort, then the subagent the turn runs as.
    await db.sql`UPDATE ai_sessions SET agent_effort = 'low' WHERE id = ${session.id}`
    await (await m.send(session.id, agentA, 'again')).done
    expect(runs).toHaveLength(2)
    expect(runs[1]?.effort).toBe('low')
    await db.sql`UPDATE ai_sessions SET agent_type = 'model-author' WHERE id = ${session.id}`
    await (await m.send(session.id, agentA, 'and again')).done
    expect(runs).toHaveLength(3)
    expect(runs[2]?.agent?.name).toBe('model-author')
    // The resync assembles the session as its turns do, so the process stays and the next turn reuses it.
    await m.revalidateWarm()
    expect(m.warm.size()).toBe(1)
    await (await m.send(session.id, agentA, 'once more')).done
    expect(runs).toHaveLength(3)
  })

  it('SessionManager.revalidateWarm keeps a process whose assembly still matches, and drops one whose credential was revoked', async () => {
    const credentials = fixedCredentials({ kind: 'anthropic_api_key', secret: 'sk-ant-test' })
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, credentials })
    stops.push(() => m.warm.close())
    const { turn: started } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
    await started!.done
    expect(m.warm.size()).toBe(1)
    await m.revalidateWarm()
    expect(m.warm.size()).toBe(1)
    // The pool now offers another credential (a rotated key: a new row and epoch).
    const [first] = await credentials.candidates()
    credentials.candidates = () => Promise.resolve([{ ...first!, id: 'rotated', epoch: 1 }])
    await m.revalidateWarm()
    expect(m.warm.size()).toBe(0)
  })

  it('a runtime key written through `set` tells every replica, as one written through `setMany` does (#3598)', async () => {
    const received: string[] = []
    const listener = await db.sql.listen(SETTINGS_CHANNEL, (payload) => {
      received.push(payload)
    })
    stops.push(() => listener.unlisten())
    await new SettingsStore(db.sql).set('warm_idle_seconds', 120)
    await expect.poll(() => received).toContain(RUNTIME_PAYLOAD)
  })
})
