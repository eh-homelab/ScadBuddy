import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { userMessage } from '../src/harness/inbox.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, type Throwaway, throwawayDatabase } from './support/postgres.js'
import { WarmBusyError } from '../src/sessions/warm.js'
import { warmHarness } from './support/warm.js'

// WarmSessions against the bundled Claude Code (spec 2026-10-13 §5.3): one process
// per session across turns, respawned on any change to what it was spawned with,
// and let go when idle or past the cap.

let skip: string | undefined
try {
  bundledCliPath()
} catch (err) {
  skip = (err as Error).message
}
if (!TEST_DATABASE_URL) skip ??= `${TEST_DATABASE_URL_ENV} is not set`

describe.skipIf(skip !== undefined)(`WarmSessions${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Throwaway
  let h: Awaited<ReturnType<typeof warmHarness>> | undefined
  beforeEach(async () => {
    db = await throwawayDatabase()
  })
  afterEach(async () => {
    await h?.close()
    h = undefined
    await db.drop()
  })

  it('reuses a process across turns', async () => {
    h = await warmHarness(db)
    const id = h.newSession()
    const first = h.assembled()
    const one = await h.turn(id, first, 1)
    expect(one.outcome).toBe('miss')
    const second = h.assembled()
    const two = await h.turn(id, second, 1 - one.result.total_cost_usd)
    expect(two.outcome).toBe('hit')
    expect(two.entry).toBe(one.entry)
    expect(h.spawns).toHaveLength(1)
    // The entry keeps the assembly it was spawned with; the unused one is released.
    expect(two.entry.turn).toBe(first)
    expect(second.release).toHaveBeenCalledTimes(1)
    expect(first.release).not.toHaveBeenCalled()
    expect(h.warm.size()).toBe(1)
  }, 60_000)

  it('recycles when the credential row changes', async () => {
    h = await warmHarness(db)
    const id = h.newSession()
    const first = h.assembled()
    const one = await h.turn(id, first, 1)
    const two = await h.turn(id, h.assembled({ credentialUpdatedAt: '2' }), 1 - one.result.total_cost_usd, h.credential('gw-new-key-9999'))
    expect(two).toMatchObject({ outcome: 'recycle', reason: 'fingerprint' })
    expect(h.spawns).toHaveLength(2)
    expect(h.spawns[1]?.env.ANTHROPIC_AUTH_TOKEN).toBe('gw-new-key-9999')
    await h.spawns[0]?.exited
    expect(first.release).toHaveBeenCalledTimes(1)
  }, 60_000)

  it('recycles when the credential it would use is another, under the same fingerprint', async () => {
    h = await warmHarness(db)
    const id = h.newSession()
    const one = await h.turn(id, h.assembled(), 1)
    const two = await h.turn(id, h.assembled(), 1 - one.result.total_cost_usd, h.credential('gw-fallback-key-8888'))
    expect(two).toMatchObject({ outcome: 'recycle', reason: 'fingerprint' })
    expect(h.spawns[1]?.env.ANTHROPIC_AUTH_TOKEN).toBe('gw-fallback-key-8888')
  }, 60_000)

  it('evicts idle entries', async () => {
    let now = 1_000
    h = await warmHarness(db, { idleMs: 200, now: () => now })
    const first = h.assembled()
    await h.turn(h.newSession(), first, 1)
    // Real time passes, but the clock says it has not been idle long enough.
    await new Promise((r) => setTimeout(r, 500))
    expect(h.warm.size()).toBe(1)
    now += 201
    await expect.poll(() => h?.warm.size()).toBe(0)
    await h.spawns[0]?.exited
    // Released once the process has ended on its own (warm.ts `drop`).
    await expect.poll(() => vi.mocked(first.release).mock.calls.length).toBe(1)
  }, 60_000)

  it('evicts LRU past maxEntries', async () => {
    h = await warmHarness(db, { maxEntries: 2 })
    const turns = [h.assembled(), h.assembled(), h.assembled()]
    for (const t of turns) await h.turn(h.newSession(), t, 1)
    expect(h.warm.size()).toBe(2)
    await h.spawns[0]?.exited
    expect(turns.map((t) => vi.mocked(t.release).mock.calls.length)).toEqual([1, 0, 0])
  }, 60_000)

  it('abandon releases a turn that ended without a result', async () => {
    h = await warmHarness(db)
    const id = h.newSession()
    const first = h.assembled()
    const warmTurn = await h.warm.acquire(id, first, h.credential(), 1)
    warmTurn.entry.inbox.push(userMessage('hello'))
    // The process dies mid-turn: the turn's iterator throws, and no result comes.
    const iterated = (async () => {
      for (;;) {
        const { done } = await warmTurn.entry.iterator.next()
        if (done) throw new Error('query ended')
      }
    })()
    await expect.poll(() => h?.spawns.length).toBe(1)
    h.spawns[0]?.child.kill('SIGKILL')
    await expect(iterated).rejects.toThrow()
    await h.warm.abandon(warmTurn.entry, 'the turn failed')
    expect(h.warm.size()).toBe(0)
    expect(first.release).toHaveBeenCalledTimes(1)
    await h.spawns[0]?.exited
    // The next turn starts a fresh process.
    const next = await h.turn(id, h.assembled(), 1)
    expect(next.outcome).toBe('miss')
  }, 60_000)

  it('refuses a second acquire while a turn holds the entry', async () => {
    h = await warmHarness(db)
    const id = h.newSession()
    const held = await h.warm.acquire(id, h.assembled(), h.credential(), 1)
    const second = h.assembled()
    await expect(h.warm.acquire(id, second, h.credential(), 1)).rejects.toBeInstanceOf(WarmBusyError)
    // The running turn's process is untouched; the refused assembly stays the caller's.
    expect(second.release).not.toHaveBeenCalled()
    const done = await h.run(held)
    expect(done.result.subtype).toBe('success')
    expect(h.spawns).toHaveLength(1)
  }, 60_000)

  it('close() ends every process', async () => {
    h = await warmHarness(db)
    const turns = [h.assembled(), h.assembled()]
    for (const t of turns) await h.turn(h.newSession(), t, 1)
    await h.warm.close()
    await Promise.all(h.spawns.map((s) => s.exited))
    expect(h.warm.size()).toBe(0)
    for (const t of turns) expect(t.release).toHaveBeenCalledTimes(1)
  }, 60_000)
})
