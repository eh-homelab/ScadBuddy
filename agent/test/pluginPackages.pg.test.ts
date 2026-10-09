import { mkdtempSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { loadPackagesForRun, PackageInstaller } from '../src/plugins/packages/install.js'
import { validateSource } from '../src/plugins/packages/source.js'
import { packageKinds } from '../src/plugins/packages/operations.js'
import { PackageStore, type PackageView } from '../src/plugins/packages/store.js'
import { PluginError } from '../src/plugins/registry.js'
import { ownPluginEnabled, SETTING_OWN_PLUGIN } from '../src/plugins/packages/builtins.js'
import { SETTING_HEADLESS_BROWSER } from '../src/harness/headlessBrowser.js'
import { SettingsStore } from '../src/credentials.js'
import { UI_ACTOR } from '../src/audit/writes.js'
import { gitMissing, gitRepo, GREETER, localFetcher, resolver, type TestRepo } from './support/gitRepo.js'
import { InlineCommands } from './support/commands.js'
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
          allowRefused: false,
        },
      ])
    })

    it('approves a pin the rules refuse only with allow_refused, and a re-pin is allowed anew', async () => {
      const hook = { Stop: [{ hooks: [{ type: 'command', command: 'id' }] }] }
      repos.greeter!.remove()
      const repo = (repos.greeter = gitRepo({ ...GREETER, 'hooks/hooks.json': JSON.stringify({ hooks: hook }) }))
      const installed = await install()
      expect(installed).toMatchObject({ approved: false, allow_refused: false })
      expect(installed.review.refused).toEqual([expect.stringMatching(/Stop has a "command" hook/)])
      await expect(store.approve('greeter', installed.commit_sha, installed.content_hash)).rejects.toMatchObject({
        status: 409,
        message: expect.stringMatching(/allow_refused/),
      })
      const approved = await store.approve('greeter', installed.commit_sha, installed.content_hash, true)
      expect(approved).toMatchObject({ approved: true, allow_refused: true })
      await store.setEnabled('greeter', true)
      expect((await store.enabledPins())[0]?.allowRefused).toBe(true)
      // Re-approving it without the flag is refused, not a silent downgrade.
      await expect(store.approve('greeter', installed.commit_sha, installed.content_hash, false)).rejects.toMatchObject({
        status: 409,
      })

      // A clean re-pin: approved without the flag, and the allowance does not carry over.
      const next = repo.commitFiles({ 'hooks/hooks.json': null })
      const current = await store.pinOf('greeter')
      const pending = await store.setPending('greeter', await installer.prepare({ ...current!.source, ref: 'main' }))
      expect(pending.pending?.review.refused).toEqual([])
      const promoted = await store.approve('greeter', next, pending.pending!.content_hash, true)
      expect(promoted).toMatchObject({ commit_sha: next, allow_refused: false, enabled: true })
      await expect(db.sql`UPDATE ai_plugin_packages SET allow_refused = true, approved_at = NULL`).rejects.toThrow(
        /check constraint/,
      )
    })

    it('shows a review stored before refusals were listed as refusing nothing', async () => {
      const installed = await install()
      await db.sql`UPDATE ai_plugin_packages SET review = review - 'refused'`
      expect((await store.get('greeter'))?.review.refused).toEqual([])
      expect((await store.approve('greeter', installed.commit_sha, installed.content_hash)).allow_refused).toBe(false)
      // A clean pin re-approved with the flag stays without it: nothing was shown to allow.
      expect((await store.approve('greeter', installed.commit_sha, installed.content_hash, true)).allow_refused).toBe(false)
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
    const appDeps = (): AppDeps => ({
        database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
        backend: () => Promise.resolve(true),
        kek: { ok: false, reason: 'not configured' },
        credentials: new MemoryCredentials(),
        pluginPackages: store,
        packageInstaller: installer,
        commands: new InlineCommands(packageKinds({ packages: store, installer })),
        testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
        remoteAddress: () => '10.0.0.7',
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      })
    const app = () => createApp(appDeps())
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

    it('lists the built-in plugins first; they enable and disable through their settings, and are never removed', async () => {
      const settings = new SettingsStore(db.sql)
      const a = createApp({ ...appDeps(), settings })
      const listed = (await (await a.request('/api/v1/ai/plugin-packages', { headers: READ })).json()) as {
        name: string
        built_in?: boolean
        enabled: boolean
        review: { skills: string[] }
      }[]
      expect(listed.map((p) => [p.name, p.built_in, p.enabled])).toEqual([
        ['scadbuddy', true, true],
        ['playwright', true, false],
      ])
      expect(listed[0]!.review.skills).toContain('scadbuddy:authoring')

      const off = await a.request('/api/v1/ai/plugin-packages/scadbuddy', json('PATCH', { enabled: false }))
      expect(((await off.json()) as { enabled: boolean }).enabled).toBe(false)
      expect(await settings.get(SETTING_OWN_PLUGIN)).toBe(false)
      expect(await ownPluginEnabled(settings)).toBe(false)
      // The browser's switch is the headless-browser setting itself.
      await a.request('/api/v1/ai/plugin-packages/playwright', json('PATCH', { enabled: true }))
      expect(await settings.get(SETTING_HEADLESS_BROWSER)).toBe(true)
      const one = await a.request('/api/v1/ai/plugin-packages/playwright', { headers: READ })
      expect(await one.json()).toMatchObject({ name: 'playwright', built_in: true, enabled: true })

      for (const [method, url, body] of [
        ['DELETE', '/api/v1/ai/plugin-packages/scadbuddy', undefined],
        ['POST', '/api/v1/ai/plugin-packages/scadbuddy/approve', { commit_sha: 'a', content_hash: 'b' }],
        ['POST', '/api/v1/ai/plugin-packages/playwright/repin', {}],
        ['DELETE', '/api/v1/ai/plugin-packages/playwright/pending', undefined],
      ] as const) {
        const res = await a.request(url, body === undefined ? { method, headers: UI } : json(method, body))
        expect(res.status, `${method} ${url}`).toBe(409)
        expect(await res.json()).toMatchObject({ built_in: true })
      }
      // Without the settings store a built-in cannot be switched: 503, never a stored row.
      const noSettings = await app().request('/api/v1/ai/plugin-packages/scadbuddy', json('PATCH', { enabled: false }))
      expect(noSettings.status).toBe(503)
    })

    it('never loads a package stored under a built-in\'s name, and lets DELETE remove it', async () => {
      // Stored before builtins.ts, when a reserved name was allowable.
      const installed = await install()
      await store.approve('greeter', installed.commit_sha, installed.content_hash)
      await store.setEnabled('greeter', true)
      await db.sql`UPDATE ai_plugin_packages SET name = 'playwright' WHERE name = 'greeter'`
      const loaded = await loadPackagesForRun(store, installer)
      expect(loaded.paths).toEqual([])
      expect(loaded.problems).toEqual([expect.stringMatching(/playwright was not loaded: "playwright" is built in/)])

      const a = createApp({ ...appDeps(), settings: new SettingsStore(db.sql) })
      const listed = (await (await a.request('/api/v1/ai/plugin-packages', { headers: READ })).json()) as {
        name: string
        built_in?: boolean
      }[]
      expect(listed.map((p) => [p.name, p.built_in ?? false])).toEqual([
        ['scadbuddy', true],
        ['playwright', true],
        ['playwright', false],
      ])
      // PATCH always switches the built-in: the stored row never loads.
      const patched = await a.request('/api/v1/ai/plugin-packages/playwright', json('PATCH', { enabled: true }))
      expect(await patched.json()).toMatchObject({ name: 'playwright', built_in: true, enabled: true })
      const del = () => a.request('/api/v1/ai/plugin-packages/playwright', { method: 'DELETE', headers: UI })
      expect((await del()).status).toBe(204)
      expect(await store.list()).toEqual([])
      expect((await del()).status).toBe(409) // the built-in stays
    })

    it("lists a package's files with their sizes and serves each one's content, the pending re-pin's too", async () => {
      await install()
      const a = app()
      const files = await a.request('/api/v1/ai/plugin-packages/greeter/files', { headers: READ })
      expect(files.status).toBe(200)
      const listed = (await files.json()) as { files: { path: string; size: number }[] }
      expect(listed.files).toContainEqual({ path: 'README.md', size: GREETER['README.md']!.length })
      expect(listed.files.map((f) => f.path)).toEqual(Object.keys(GREETER).sort())

      const read = (q: string) => a.request(`/api/v1/ai/plugin-packages/greeter/file?${q}`, { headers: READ })
      const readme = await read('path=README.md')
      expect(readme.status).toBe(200)
      expect(await readme.json()).toEqual({
        path: 'README.md',
        size: GREETER['README.md']!.length,
        binary: false,
        media_type: 'text/markdown',
        truncated: false,
        content: GREETER['README.md'],
      })
      // Only a path the pin lists: never one outside the package, or a file it does not have.
      for (const q of ['path=../../etc/passwd', 'path=%2Fetc%2Fpasswd', 'path=missing.md', '']) {
        expect((await read(q)).status, q).toBe(404)
      }
      expect((await read('path=README.md&pending=true')).status).toBe(404) // no re-pin yet

      repos.greeter!.commitFiles({ 'commands/wave.md': 'Wave twice.\n', 'NOTES.md': 'new\n' })
      await a.request('/api/v1/ai/plugin-packages/greeter/repin', json('POST', { ref: 'main' }))
      const pending = await read('path=NOTES.md&pending=true')
      expect(await pending.json()).toMatchObject({ content: 'new\n' })
      expect(await (await read('path=commands/wave.md&pending=true')).json()).toMatchObject({ content: 'Wave twice.\n' })
      expect(await (await read('path=commands/wave.md')).json()).toMatchObject({ content: GREETER['commands/wave.md'] })
      expect((await read('path=NOTES.md')).status).toBe(404)
      const pendingList = (await (
        await a.request('/api/v1/ai/plugin-packages/greeter/files?pending=true', { headers: READ })
      ).json()) as { files: { path: string }[] }
      expect(pendingList.files.map((f) => f.path)).toContain('NOTES.md')

      expect((await a.request('/api/v1/ai/plugin-packages/nope/files', { headers: READ })).status).toBe(404)
      const crossSite = await a.request('/api/v1/ai/plugin-packages/greeter/file?path=README.md', {
        headers: { ...READ, 'sec-fetch-site': 'cross-site' },
      })
      expect(crossSite.status).toBe(403)
    })

    it("serves a built-in plugin's files, and has no re-pin of one", async () => {
      const a = createApp({ ...appDeps(), settings: new SettingsStore(db.sql) })
      const listed = (await (await a.request('/api/v1/ai/plugin-packages/scadbuddy/files', { headers: READ })).json()) as {
        files: { path: string }[]
      }
      expect(listed.files.map((f) => f.path)).toContain('skills/authoring/SKILL.md')
      const skill = await a.request('/api/v1/ai/plugin-packages/scadbuddy/file?path=skills/authoring/SKILL.md', {
        headers: READ,
      })
      expect(await skill.json()).toMatchObject({ binary: false, media_type: 'text/markdown', content: expect.stringMatching(/^---/) })
      const outside = await a.request('/api/v1/ai/plugin-packages/scadbuddy/file?path=../package.json', { headers: READ })
      expect(outside.status).toBe(404)
      const pending = await a.request('/api/v1/ai/plugin-packages/scadbuddy/files?pending=true', { headers: READ })
      expect(pending.status).toBe(404)
    })

    it('answers an install of a built-in plugin with 409 built_in, not a refusal', async () => {
      repos.greeter!.remove()
      repos.greeter = gitRepo({ ...GREETER, '.claude-plugin/plugin.json': JSON.stringify({ name: 'scadbuddy' }) })
      const res = await app().request('/api/v1/ai/plugin-packages', json('POST', { source: { kind: 'git', url: GIT_URL } }))
      expect(res.status).toBe(409)
      expect(await res.json()).toMatchObject({ built_in: true, detail: expect.stringMatching(/built in/) })
      expect(await store.list()).toEqual([])
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

    it('installs a package the rules refuse, and approves it only with allow_refused', async () => {
      repos.bad = gitRepo({
        ...GREETER,
        '.claude-plugin/plugin.json': JSON.stringify({ name: 'bad' }),
        'hooks/hooks.json': JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'command', command: 'id' }] }] } }),
      })
      const res = await app().request(
        '/api/v1/ai/plugin-packages',
        json('POST', { source: { kind: 'git', url: 'https://git.test/bad.git' } }),
      )
      expect(res.status).toBe(201)
      const installed = (await res.json()) as PackageView
      expect(installed.review.refused).toEqual([expect.stringMatching(/Stop has a "command" hook/)])
      const pin = { commit_sha: installed.commit_sha, content_hash: installed.content_hash }
      const plain = await app().request('/api/v1/ai/plugin-packages/bad/approve', json('POST', pin))
      expect(plain.status).toBe(409)
      const allowed = await app().request(
        '/api/v1/ai/plugin-packages/bad/approve',
        json('POST', { ...pin, allow_refused: true }),
      )
      expect(allowed.status).toBe(200)
      expect(await allowed.json()).toMatchObject({ approved: true, allow_refused: true })
    })

    it('answers 422 with every problem for a package no approval can allow, and stores nothing', async () => {
      repos.bad = gitRepo({ ...GREETER, '.claude-plugin/plugin.json': JSON.stringify({ name: 'bad name' }) })
      const res = await app().request(
        '/api/v1/ai/plugin-packages',
        json('POST', { source: { kind: 'git', url: 'https://git.test/bad.git' } }),
      )
      expect(res.status).toBe(422)
      expect(((await res.json()) as { problems: string[] }).problems).toEqual([
        expect.stringMatching(/^plugin name "bad name" is not 1–64/),
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

    it('loads ScadBuddy\'s own plugin unless Settings switched it off', async () => {
      const settings = new SettingsStore(db.sql)
      const runs: HarnessRun[] = []
      const scripted = scriptedRunner(() => ({ reply: 'hi' }))
      const m = manager({
        sql: db.sql,
        paths: await tempPaths(),
        settings,
        ownPlugin: '/opt/own-plugin',
        run: (run) => {
          runs.push(run)
          return scripted.runner(run)
        },
      })
      await (await m.start(agentA, { origin: 'mcp', prompt: 'hi' })).turn!.done
      expect(runs[0]?.ownPlugin).toBe('/opt/own-plugin')

      await settings.set(SETTING_OWN_PLUGIN, false, { actor: UI_ACTOR, surface: 'http' })
      await (await m.start(agentA, { origin: 'mcp', prompt: 'hi' })).turn!.done
      expect(runs[1]?.ownPlugin).toBeUndefined()
    })

    it('hands a package approved with allow_refused to the turn as allowed, not vetted', async () => {
      repos.greeter!.remove()
      repos.greeter = gitRepo({
        ...GREETER,
        'hooks/hooks.json': JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'command', command: 'id' }] }] } }),
      })
      const installed = await install()
      await store.approve('greeter', installed.commit_sha, installed.content_hash, true)
      await store.setEnabled('greeter', true)
      const scripted = scriptedRunner(() => ({ reply: 'hi' }))
      const runs: HarnessRun[] = []
      const m = manager({
        sql: db.sql,
        paths: await tempPaths(),
        run: (run) => {
          runs.push(run)
          return scripted.runner(run)
        },
        packagePlugins: () => loadPackagesForRun(store, installer),
      })
      const { turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
      await turn!.done
      expect(runs[0]?.pluginPaths).toBeUndefined()
      expect(runs[0]?.allowedPluginPaths).toEqual([installer.cacheDir((await store.enabledPins())[0]!)])
    })
  })
})
