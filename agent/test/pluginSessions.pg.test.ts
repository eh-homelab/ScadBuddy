import { randomBytes } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { endOwnSessions, PluginSessions, runPluginSessionSweep, startMark, sweepDeadReplicas } from '../src/plugins/pluginSessions.js'
import { PluginStore } from '../src/plugins/registry.js'
import { kekFromBase64 } from '../src/secrets.js'
import { type FakeMcp, startFakeMcp } from './support/fakeMcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The upstream MCP sessions a dead replica left open (migration plugin_sessions).

const kek = kekFromBase64(randomBytes(32).toString('base64'))

describe.skipIf(!TEST_DATABASE_URL)(
  `plugin sessions on Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let fake: FakeMcp
    let store: PluginStore
    const left = async () =>
      (await db.sql<{ plugin_id: string; upstream_session_id: string }[]>`
        SELECT plugin_id, upstream_session_id FROM ai_plugin_sessions ORDER BY upstream_session_id`).map(
        (r) => `${r.plugin_id}/${r.upstream_session_id}`,
      )
    const row = (plugin: string, id: string, replica: string) =>
      db.sql`INSERT INTO ai_plugin_sessions (plugin_id, upstream_session_id, replica) VALUES (${plugin}, ${id}, ${replica})`

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      fake = await startFakeMcp({ sessions: true })
      store = new PluginStore(db.sql)
      await store.create({ name: 'memory', url: fake.url, secret: 'Bearer sweep-secret-1' }, kek)
      await store.update('memory', { enabled: true }, kek)
    })
    afterEach(async () => {
      await fake.close()
      await drop()
    })

    it("the sweep deletes a dead replica's sessions and keeps a live one's", async () => {
      await row('memory', 'dead-1', 'replica-dead')
      await row('memory', 'live-1', 'replica-live')
      const removed = await sweepDeadReplicas(db.sql, new Set(['replica-live']), { plugins: store, kek, graceMs: 0 })
      expect(removed).toBe(1)
      expect(fake.deletes.map((d) => d.sessionId)).toEqual(['dead-1'])
      expect(fake.deletes[0]?.headers.authorization).toBe('Bearer sweep-secret-1')
      expect(await left()).toEqual(['memory/live-1'])
    })

    it("forgets a session of a plugin that is gone or disabled without sending a DELETE", async () => {
      await store.create({ name: 'off', url: fake.url }, undefined)
      await row('nope', 'a', 'replica-dead')
      await row('off', 'b', 'replica-dead')
      await sweepDeadReplicas(db.sql, new Set(), { plugins: store, kek, graceMs: 0 })
      expect(fake.deletes).toEqual([])
      expect(await left()).toEqual([])
    })

    it("sends the sweep's DELETE to the checked address, never resolving the plugin's name", async () => {
      await store.create({ name: 'pinned', url: fake.url.replace('127.0.0.1', 'hostname.invalid'), secret: 'Bearer pin-secret' }, kek)
      await store.update('pinned', { enabled: true }, kek)
      await row('pinned', 'p-1', 'replica-dead')
      await sweepDeadReplicas(db.sql, new Set(), { plugins: store, kek, graceMs: 0, resolve: async () => ['127.0.0.1'] })
      expect(fake.deletes.map((d) => d.sessionId)).toEqual(['p-1'])
      expect(await left()).toEqual([])
    })

    it('sends nothing for a plugin the egress check refuses, and drops its rows', async () => {
      await store.create({ name: 'refused', url: fake.url.replace('127.0.0.1', 'refused.example') }, undefined)
      await store.update('refused', { enabled: true }, undefined)
      await row('refused', 'm-1', 'replica-dead')
      const log: string[] = []
      await sweepDeadReplicas(db.sql, new Set(), {
        plugins: store,
        kek,
        graceMs: 0,
        resolve: async () => ['169.254.169.254'],
        log: (m) => log.push(m),
      })
      expect(fake.deletes).toEqual([])
      expect(await left()).toEqual([])
      expect(log.join('\n')).toContain('refused is not an enabled plugin that passes the egress check')
    })

    it('keeps the row of a DELETE that failed for the next sweep, and gives up on one older than 24 h', async () => {
      await store.create({ name: 'down', url: 'http://127.0.0.1:1/mcp' }, undefined)
      await store.update('down', { enabled: true }, undefined)
      await row('down', 'recent', 'replica-dead')
      await row('down', 'ancient', 'replica-dead')
      await db.sql`UPDATE ai_plugin_sessions SET created_at = now() - interval '25 hours' WHERE upstream_session_id = 'ancient'`
      await sweepDeadReplicas(db.sql, new Set(), { plugins: store, kek: undefined, graceMs: 0 })
      expect(await left()).toEqual(['down/recent'])
    })

    it("at start, ends this replica's own rows from before, failures included, and no one else's", async () => {
      await store.create({ name: 'down', url: 'http://127.0.0.1:1/mcp' }, undefined)
      await store.update('down', { enabled: true }, undefined)
      await row('memory', 'old-me', 'me')
      await row('down', 'old-me-failing', 'me')
      await row('memory', 'other', 'other')
      const mark = await startMark(db.sql)
      expect(await endOwnSessions(db.sql, 'me', { plugins: store, kek }, mark)).toBe(2)
      expect(fake.deletes.map((d) => d.sessionId)).toEqual(['old-me'])
      expect(await left()).toEqual(['memory/other'])
    })

    it("at start, keeps a row this incarnation recorded after the start mark", async () => {
      await row('memory', 'before', 'me')
      const mark = await startMark(db.sql)
      await new PluginSessions(db.sql, 'me').seen('memory', 'after')
      expect(await endOwnSessions(db.sql, 'me', { plugins: store, kek }, mark)).toBe(1)
      expect(fake.deletes.map((d) => d.sessionId)).toEqual(['before'])
      expect(await left()).toEqual(['memory/after'])
    })

    it("leaves a young row alone: its replica's worker may not poll yet", async () => {
      await row('memory', 'young', 'replica-new')
      expect(await sweepDeadReplicas(db.sql, new Set(), { plugins: store, kek })).toBe(0)
      expect(fake.deletes).toEqual([])
      expect(await left()).toEqual(['memory/young'])
    })

    it('records a session once and forgets it', async () => {
      const sessions = new PluginSessions(db.sql, 'replica-a')
      await sessions.seen('memory', 's')
      await sessions.seen('memory', 's')
      expect(await left()).toEqual(['memory/s'])
      await sessions.ended('memory', 's')
      expect(await left()).toEqual([])
    })

    describe('a run', () => {
      const describing = (identities: string[] | Error) => ({
        options: { namespace: 'default' },
        withDeadline: <T>(_d: number, fn: () => Promise<T>) => fn(),
        workflowService: {
          describeTaskQueue: async () => {
            if (identities instanceof Error) throw identities
            return { pollers: identities.map((identity) => ({ identity })) }
          },
        },
      })
      const run = (client: unknown, log: string[]) =>
        runPluginSessionSweep({
          sql: db.sql,
          client: client as never,
          plugins: store,
          kek,
          graceMs: 0,
          self: 'me',
          log: (m) => log.push(m),
        })

      it('takes the pollers (pid@host) and itself as live, and sweeps the rest', async () => {
        await row('memory', 'mine', 'me')
        await row('memory', 'theirs', 'other')
        await row('memory', 'dead', 'gone')
        const log: string[] = []
        await run(describing(['41@other']), log)
        expect(await left()).toEqual(['memory/mine', 'memory/theirs'])
        expect(fake.deletes.map((d) => d.sessionId)).toEqual(['dead'])
      })

      it('skips the run, and logs it, when DescribeTaskQueue fails', async () => {
        await row('memory', 'theirs', 'other')
        const log: string[] = []
        await run(describing(new Error('unavailable')), log)
        expect(await left()).toEqual(['memory/theirs'])
        expect(fake.deletes).toEqual([])
        expect(log.join('\n')).toContain('DescribeTaskQueue failed: unavailable')
      })

      it('skips the run when nobody polls, and when Temporal is not configured', async () => {
        await row('memory', 'theirs', 'other')
        const log: string[] = []
        await run(describing([]), log)
        await run(undefined, log)
        expect(await left()).toEqual(['memory/theirs'])
        expect(fake.deletes).toEqual([])
        expect(log).toHaveLength(2)
      })
    })
  },
)
