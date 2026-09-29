import { mkdtempSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { loadPackagesForRun, PackageInstaller } from '../src/plugins/packages/install.js'
import { validateSource } from '../src/plugins/packages/source.js'
import { PackageStore, type PackageView } from '../src/plugins/packages/store.js'
import { PluginError } from '../src/plugins/registry.js'
import { gitMissing, gitRepo, GREETER, localFetcher, resolver, type TestRepo } from './support/gitRepo.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// Plugin packages (#297) against real Postgres: the pin lifecycle in
// `ai_plugin_packages` (install → approve → enable, re-pin with a diff), the
// routes over it, and the session manager loading enabled packages into a
// turn. The git side uses local repositories (test/support/gitRepo.ts).

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const GIT_URL = 'https://git.test/greeter.git'

const skip = !TEST_DATABASE_URL || gitMissing !== undefined
const why = !TEST_DATABASE_URL ? `${TEST_DATABASE_URL_ENV} is not set` : (gitMissing ?? '')

describe.skipIf(skip)(`plugin packages on Postgres${skip ? ` (skipped: ${why})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let store: PackageStore
  let repos: Record<string, TestRepo>
  let cacheRoot: string
  let installer: PackageInstaller

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new PackageStore(db.sql)
    repos = { greeter: gitRepo(GREETER) }
    cacheRoot = mkdtempSync(path.join(os.tmpdir(), 'pkg-pg-cache-'))
    installer = new PackageInstaller({ fetcher: localFetcher(repos), cacheRoot, resolve: resolver() })
  })
  afterEach(async () => {
    await drop()
    for (const repo of Object.values(repos)) repo.remove()
    rmSync(cacheRoot, { recursive: true, force: true })
  })

  const install = async () => store.create(await installer.prepare(validateSource({ kind: 'git', url: GIT_URL })))

  describe('the store', () => {
    it('stores an install unapproved and disabled; enabling needs the approval of exactly that pin', async () => {
      const installed = await install()
      expect(installed).toMatchObject({
        name: 'greeter',
        source: { kind: 'git', url: GIT_URL, ref: 'HEAD', path: '' },
        commit_sha: repos.greeter!.commit,
        approved: false,
        enabled: false,
        pending: null,
      })
      await expect(store.setEnabled('greeter', true)).rejects.toMatchObject({ status: 409 })
      await expect(store.approve('greeter', installed.commit_sha, `sha256:${'0'.repeat(64)}`)).rejects.toMatchObject({
        status: 409,
      })
      await expect(store.approve('greeter', 'f'.repeat(40), installed.content_hash)).rejects.toMatchObject({ status: 409 })
      expect(await store.enabledPins()).toEqual([])

      const approved = await store.approve('greeter', installed.commit_sha, installed.content_hash)
      expect(approved.approved).toBe(true)
      expect((await store.setEnabled('greeter', true)).enabled).toBe(true)
      expect(await store.enabledPins()).toEqual([
        {
          name: 'greeter',
          fetchUrl: GIT_URL,
          fetchPath: '',
          commit: installed.commit_sha,
          contentHash: installed.content_hash,
        },
      ])
    })

    it('refuses a second install of the same name, and the table refuses an unapproved enable', async () => {
      await install()
      await expect(install()).rejects.toMatchObject({ status: 409 })
      await expect(db.sql`UPDATE ai_plugin_packages SET enabled = true WHERE name = 'greeter'`).rejects.toThrow(
        /check constraint/,
      )
    })

    it('re-pins to a new commit as pending, with a diff; the old pin loads until the new one is approved', async () => {
      const installed = await install()
      await store.approve('greeter', installed.commit_sha, installed.content_hash)
      await store.setEnabled('greeter', true)
      const next = repos.greeter!.commitFiles({
        'skills/hello/SKILL.md': '---\ndescription: New.\n---\n\nHello again.\n',
        'skills/bye/SKILL.md': '---\ndescription: Bye.\n---\n\nBye.\n',
        'README.md': null,
      })
      const current = await store.pinOf('greeter')
      const pending = await store.setPending('greeter', await installer.prepare({ ...current!.source, ref: 'main' }))
      expect(pending.pending).toMatchObject({
        ref: 'main',
        commit_sha: next,
        diff: { added: ['skills/bye/SKILL.md'], removed: ['README.md'], changed: ['skills/hello/SKILL.md'] },
        review: { skills: ['greeter:bye', 'greeter:hello'] },
      })
      expect((await store.enabledPins())[0]?.commit).toBe(installed.commit_sha)

      // Re-pinning to what is already pinned is refused.
      await expect(
        store.setPending('greeter', await installer.prepare({ ...current!.source, ref: installed.commit_sha })),
      ).rejects.toMatchObject({ status: 409 })

      const promoted = await store.approve('greeter', next, pending.pending!.content_hash)
      expect(promoted).toMatchObject({ commit_sha: next, enabled: true, pending: null, source: { ref: 'main' } })
      expect((await store.enabledPins())[0]?.commit).toBe(next)
    })

    it('re-pins a marketplace entry that moved to another repository, and loads from the new one', async () => {
      const market = (url: string) =>
        JSON.stringify({ name: 'm', owner: { name: 'o' }, plugins: [{ name: 'greeter', source: { source: 'url', url } }] })
      repos.market = gitRepo({ '.claude-plugin/marketplace.json': market('https://git.test/greeter.git') })
      const source = validateSource({ kind: 'marketplace', url: 'https://git.test/market.git', entry: 'greeter', ref: 'main' })
      const installed = await store.create(await installer.prepare(source))
      await store.approve('greeter', installed.commit_sha, installed.content_hash)
      await store.setEnabled('greeter', true)

      // The entry moves to another repository, whose history the old one lacks.
      repos.moved = gitRepo({ ...GREETER, 'skills/hello/SKILL.md': '---\ndescription: Moved.\n---\n\nHi.\n' })
      repos.market.commitFiles({ '.claude-plugin/marketplace.json': market('https://git.test/moved.git') })
      const pending = await store.setPending('greeter', await installer.prepare((await store.pinOf('greeter'))!.source))
      expect(pending.pending).toMatchObject({ plugin_url: 'https://git.test/moved.git', plugin_path: '', commit_sha: repos.moved.commit })
      expect((await store.enabledPins())[0]?.fetchUrl).toBe('https://git.test/greeter.git') // until approved

      const approved = await store.approve('greeter', repos.moved.commit, pending.pending!.content_hash)
      expect(approved.source).toMatchObject({ plugin_url: 'https://git.test/moved.git' })
      // A new source is approved disabled: it loads only once enabled again.
      expect(approved.enabled).toBe(false)
      expect(await store.enabledPins()).toEqual([])
      await store.setEnabled('greeter', true)
      expect((await store.enabledPins())[0]).toMatchObject({ fetchUrl: 'https://git.test/moved.git', commit: repos.moved.commit })
      rmSync(cacheRoot, { recursive: true, force: true }) // a fresh replica: fetched from the pin
      const loaded = await loadPackagesForRun(store, installer)
      expect(loaded.problems).toEqual([])
      expect(loaded.paths).toHaveLength(1)
      loaded.release()
    })

    it('discards a pending re-pin, and deletes', async () => {
      await install()
      repos.greeter!.commitFiles({ 'skills/hello/SKILL.md': 'v2\n' })
      await store.setPending('greeter', await installer.prepare(validateSource({ kind: 'git', url: GIT_URL, ref: 'main' })))
      expect((await store.discardPending('greeter')).pending).toBeNull()
      expect(await store.delete('greeter')).toBe(true)
      expect(await store.delete('greeter')).toBe(false)
      await expect(store.discardPending('greeter')).rejects.toBeInstanceOf(PluginError)
    })
  })

  describe('the routes', () => {
    const app = () =>
      createApp({
        database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
        backend: () => Promise.resolve(true),
        kek: { ok: false, reason: 'not configured' },
        credentials: new MemoryCredentials(),
        pluginPackages: store,
        packageInstaller: installer,
        testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
        remoteAddress: () => '10.0.0.7',
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      })
    const json = (method: string, body: unknown, headers: Record<string, string> = UI) => ({
      method,
      headers: { ...headers, 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })

    it('installs, shows the review, approves what it showed, enables, re-pins and uninstalls', async () => {
      const a = app()
      const res = await a.request('/api/v1/ai/plugin-packages', json('POST', { source: { kind: 'git', url: GIT_URL } }))
      expect(res.status).toBe(201)
      const installed = (await res.json()) as PackageView
      expect(installed.review.mcp_servers).toEqual([{ name: 'mem', type: 'http', url: 'https://mcp.example/mcp/' }])

      const listed = (await (await a.request('/api/v1/ai/plugin-packages', { headers: READ })).json()) as PackageView[]
      expect(listed.map((p) => [p.name, p.approved, p.enabled])).toEqual([['greeter', false, false]])

      expect((await a.request('/api/v1/ai/plugin-packages/greeter', json('PATCH', { enabled: true }))).status).toBe(409)
      const approve = await a.request(
        '/api/v1/ai/plugin-packages/greeter/approve',
        json('POST', { commit_sha: installed.commit_sha, content_hash: installed.content_hash }),
      )
      expect(approve.status).toBe(200)
      const enabled = await a.request('/api/v1/ai/plugin-packages/greeter', json('PATCH', { enabled: true }))
      expect(((await enabled.json()) as PackageView).enabled).toBe(true)

      repos.greeter!.commitFiles({ 'commands/wave.md': 'Wave twice.\n' })
      const repin = await a.request('/api/v1/ai/plugin-packages/greeter/repin', json('POST', { ref: 'main' }))
      expect(repin.status).toBe(200)
      expect(((await repin.json()) as PackageView).pending?.diff.changed).toEqual(['commands/wave.md'])
      expect((await a.request('/api/v1/ai/plugin-packages/greeter/pending', { method: 'DELETE', headers: UI })).status).toBe(
        200,
      )

      const dir = path.join(cacheRoot, 'greeter')
      const del = await a.request('/api/v1/ai/plugin-packages/greeter', { method: 'DELETE', headers: UI })
      expect(del.status).toBe(204)
      expect(await store.list()).toEqual([])
      expect(() => rmSync(dir)).toThrow() // evicted from the cache
    })

    it('refuses writes and reads from outside the UI, and plain-text bodies', async () => {
      const a = app()
      const body = JSON.stringify({ source: { kind: 'git', url: GIT_URL } })
      const noOrigin = await a.request('/api/v1/ai/plugin-packages', {
        method: 'POST',
        headers: { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' },
        body,
      })
      expect(noOrigin.status).toBe(403)
      const text = await a.request('/api/v1/ai/plugin-packages', {
        method: 'POST',
        headers: { ...UI, 'content-type': 'text/plain' },
        body,
      })
      expect(text.status).toBe(403)
      const crossSite = await a.request('/api/v1/ai/plugin-packages', {
        headers: { ...READ, 'sec-fetch-site': 'cross-site' },
      })
      expect(crossSite.status).toBe(403)
      expect(await store.list()).toEqual([])
    })

    it('answers 422 with every problem for a refused package, and stores nothing', async () => {
      repos.bad = gitRepo({
        ...GREETER,
        'hooks/hooks.json': JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'command', command: 'id' }] }] } }),
      })
      const res = await app().request(
        '/api/v1/ai/plugin-packages',
        json('POST', { source: { kind: 'git', url: 'https://git.test/bad.git' } }),
      )
      expect(res.status).toBe(422)
      expect(((await res.json()) as { problems: string[] }).problems).toEqual([
        expect.stringMatching(/Stop has a "command" hook/),
      ])
      expect(await store.list()).toEqual([])
    })

    it('answers 400 for a bad source', async () => {
      const res = await app().request(
        '/api/v1/ai/plugin-packages',
        json('POST', { source: { kind: 'git', url: 'ssh://git@github.com/o/r.git' } }),
      )
      expect(res.status).toBe(400)
    })
  })

  describe('the session manager', () => {
    it('loads the enabled packages into each turn by path, and reports the ones it cannot load', async () => {
      const installed = await install()
      await store.approve('greeter', installed.commit_sha, installed.content_hash)
      await store.setEnabled('greeter', true)
      // A second, enabled package whose pin the repository cannot satisfy.
      repos.other = gitRepo({ ...GREETER, '.claude-plugin/plugin.json': JSON.stringify({ name: 'other' }) })
      const other = await store.create(
        await installer.prepare(validateSource({ kind: 'git', url: 'https://git.test/other.git' })),
      )
      await store.approve('other', other.commit_sha, other.content_hash)
      await store.setEnabled('other', true)
      await db.sql`UPDATE ai_plugin_packages SET fetch_url = 'https://git.test/gone.git' WHERE name = 'other'`
      rmSync(path.join(cacheRoot, 'other'), { recursive: true, force: true })

      const scripted = scriptedRunner(() => ({ reply: 'hi' }))
      const runs: HarnessRun[] = []
      const runner = (run: HarnessRun): AsyncIterable<SDKMessage> =>
        (async function* () {
          runs.push(run)
          yield { type: 'system', subtype: 'init', mcp_servers: [], plugins: [] } as unknown as SDKMessage
          yield* scripted.runner(run)
        })()
      const m = manager({
        sql: db.sql,
        paths: await tempPaths(),
        run: runner,
        packagePlugins: () => loadPackagesForRun(store, installer),
      })
      const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
      await turn!.done
      expect(runs[0]?.pluginPaths).toEqual([installer.cacheDir({ ...(await store.enabledPins())[0]! })])

      const logged = (
        await db.sql<{ event: string }[]>`
          SELECT event FROM ai_session_events WHERE session_id = ${session.id} ORDER BY seq`
      ).map((e) => JSON.parse(e.event) as { type: string; code?: string; message?: string })
      expect(
        logged.filter((e) => e.type === 'error' && e.code === 'plugin_unavailable').map((e) => e.message),
      ).toEqual([
        expect.stringMatching(/^plugin package other was not loaded: no test repository/),
        // The fake init message lists no plugins: the loaded one is reported as not loaded by Claude Code.
        'plugin package greeter was not loaded by Claude Code',
      ])
    })
  })
})
