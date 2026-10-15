import { type ChildProcess, spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import type { SDKMessage, SDKResultMessage, SessionKey, SessionStore, SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'
import { vi } from 'vitest'
import type { Credential } from '../../src/credentials.js'
import { userMessage } from '../../src/harness/inbox.js'
import { ensureStateDirs } from '../../src/harness/stateDirs.js'
import { PostgresSessionStore } from '../../src/sessions/store.js'
import type { AssembledTurn, FingerprintParts } from '../../src/sessions/turnRunner.js'
import { createTurnSlot } from '../../src/sessions/turnSlot.js'
import { type WarmEntry, WarmSessions } from '../../src/sessions/warm.js'
import { type FakeAnthropic, startFakeAnthropic } from './fakeAnthropic.js'
import { tempPaths } from './sessions.js'
import { testPayloadKeys } from './payloadKeys.js'
import type { Throwaway } from './postgres.js'

// WarmSessions (spec 2026-10-13 §5.3) over the bundled Claude Code, the fake
// endpoint and a throwaway Postgres: each turn is assembled by hand (only what
// a spawn needs), so a test changes one input at a time.

export const MODEL = 'claude-sonnet-4-5'
export const KEY = 'gw-warm-test-token-0000111122223333'

export type Spawned = { env: Record<string, string | undefined>; child: ChildProcess; exited: Promise<unknown> }

export type WarmHarnessOptions = {
  maxEntries?: number
  idleMs?: number
  now?: () => number
  /** Wraps the Postgres client WarmSessions is given (the store keeps the real one). */
  sql?: (sql: Sql) => Sql
  /** Runs at each model request, before the fake replies. */
  onRequest?: () => Promise<void>
  /** Runs at each transcript append the process hands over, before it is written. */
  beforeAppend?: (key: SessionKey, entries: SessionStoreEntry[]) => Promise<void>
  log?: (message: string) => void
}

export async function warmHarness(db: Throwaway, options: WarmHarnessOptions = {}) {
  const fake: FakeAnthropic = await startFakeAnthropic(async () => {
    await options.onRequest?.()
    return { text: 'ok' }
  })
  const paths = await tempPaths()
  await ensureStateDirs(paths)
  const store = new PostgresSessionStore(db.db.sql, { keys: testPayloadKeys(db.db.sql) })
  const beforeAppend = options.beforeAppend
  const sessionStore: SessionStore = beforeAppend
    ? Object.assign(Object.create(store) as PostgresSessionStore, {
        append: async (key: SessionKey, entries: SessionStoreEntry[]) => {
          await beforeAppend(key, entries)
          await store.append(key, entries)
        },
      })
    : store
  const spawns: Spawned[] = []
  const warm = new WarmSessions({
    sql: options.sql ? options.sql(db.db.sql) : db.db.sql,
    store,
    maxEntries: options.maxEntries ?? 8,
    idleMs: options.idleMs ?? 600_000,
    ...(options.now ? { now: options.now } : {}),
    ...(options.log ? { log: options.log } : {}),
  })
  const credential = (secret = KEY): Credential => ({ kind: 'gateway', baseUrl: fake.url, secret })
  const parts: FingerprintParts = {
    credentialId: 'c1',
    credentialUpdatedAt: '1',
    model: MODEL,
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
    servedBuiltins: [],
    backgroundTasks: false,
    effort: null,
    agentType: null,
  }
  /** A turn's assembly, as TurnRunner.assemble gives it; `release` is a spy. */
  const assembled = (change: Partial<FingerprintParts> = {}): AssembledTurn => ({
    run: {
      paths,
      sessionStore,
      maxTurns: 5,
      model: MODEL,
      spawnClaudeCodeProcess: (o) => {
        const child = spawn(o.command, o.args, { cwd: o.cwd, env: o.env, stdio: ['pipe', 'pipe', 'ignore'] })
        spawns.push({ env: o.env, child, exited: new Promise((r) => child.once('exit', r)) })
        return child
      },
    },
    candidates: [],
    fingerprintParts: { ...parts, ...change },
    slot: createTurnSlot(),
    secrets: [],
    eventTierOf: () => undefined,
    shownTierOf: () => undefined,
    observe: () => Promise.resolve(),
    terminate: vi.fn(async () => {}),
    release: vi.fn(),
  })
  /** One turn on an acquired entry: push, read to the `result`, settle. */
  const run = async <T extends { entry: WarmEntry }>(warmTurn: T) => {
    warmTurn.entry.inbox.push(userMessage('hello'))
    const result = await nextResult(warmTurn.entry.iterator)
    await warm.settle(warmTurn.entry, result)
    return { ...warmTurn, result }
  }
  /** One turn: acquire, push, read to the `result`, settle. */
  const turn = async (sessionId: string, assembly: AssembledTurn, remainingUsd: number, cred = credential()) =>
    run(await warm.acquire(sessionId, assembly, cred, remainingUsd))
  return {
    warm,
    fake,
    store,
    spawns,
    credential,
    assembled,
    turn,
    run,
    newSession: () => randomUUID(),
    close: async () => {
      await warm.close()
      await fake.close()
    },
  }
}

export async function nextResult(it: AsyncIterator<SDKMessage>): Promise<SDKResultMessage> {
  for (;;) {
    const { value, done } = await it.next()
    if (done) throw new Error('query ended')
    if (value.type === 'result') return value
  }
}
