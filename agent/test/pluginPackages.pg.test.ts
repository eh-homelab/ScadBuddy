import { existsSync, mkdtempSync, readdirSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import type { HarnessRun } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { hashTree } from '../src/plugins/packages/hash.js'
import { loadPackagesForRun, PackageInstaller, verifiedReview } from '../src/plugins/packages/install.js'
import { validateSource } from '../src/plugins/packages/source.js'
import { packageKinds } from '../src/plugins/packages/operations.js'
import { PackageStore, type PackageView } from '../src/plugins/packages/store.js'
import { PluginError } from '../src/plugins/registry.js'
import { ownPluginEnabled } from '../src/plugins/packages/builtins.js'
import { BUILTIN_SOURCES } from '../src/access/model.js'
import { AccessResolver } from '../src/access/resolver.js'
import { AccessStore } from '../src/access/store.js'
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

  // #4757 Task 7: a package's `ai_access` rows and listings over its lifecycle.
  describe("a package's access rows", () => {
    const seed = async (source: string, items: Record<string, string>) => {
      for (const [item, access] of Object.entries(items)) {
        await db.sql`INSERT INTO ai_access (source, item, access, updated_by) VALUES (${source}, ${item}, ${access}, 'test')`
      }
    }
    const list = async (source: string) => {
      await db.sql`INSERT INTO ai_tool_listings (source, tools) VALUES (${source}, ${db.sql.json([])})`
    }
    const accessRows = async () =>
      (await db.sql<{ source: string; item: string; access: string }[]>`
        SELECT source, item, access FROM ai_access WHERE source LIKE 'package:%'`)
        .map((r) => `${r.source} ${r.item}=${r.access}`)
        .sort()
    const listings = async () =>
      (await db.sql<{ source: string }[]>`SELECT source FROM ai_tool_listings`).map((r) => r.source).sort()
    const installApproved = async () => {
      const installed = await install()
      return store.approve('greeter', installed.commit_sha, installed.content_hash)
    }
    const repin = async (name = 'greeter') => {
      const current = await store.pinOf(name)
      const pending = await store.setPending(name, await installer.prepare({ ...current!.source, ref: 'main' }))
      return store.approve(name, pending.pending!.commit_sha, pending.pending!.content_hash)
    }
    const hookRow = (view: PackageView) => `hook:Stop:${view.review.hooks[0]!.id!}`

    it('an override survives a re-pin that keeps the hook, and a changed hook comes back on', async () => {
      const first = await installApproved()
      await seed('package:greeter', { [hookRow(first)]: 'off' })

      repos.greeter!.commitFiles({ 'skills/hello/SKILL.md': '---\ndescription: New.\n---\n\nHello again.\n' })
      const kept = await repin()
      expect(hookRow(kept)).toBe(hookRow(first))
      expect(await accessRows()).toEqual([`package:greeter ${hookRow(first)}=off`])

      repos.greeter!.commitFiles({
        'hooks/hooks.json': JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'prompt', prompt: 'Was the user waved at?' }] }] } }),
      })
      const changed = await repin()
      expect(hookRow(changed)).not.toBe(hookRow(first))
      expect(await accessRows()).toEqual([])
    })

    it('a re-pin drops the rows of a skill and a server the new pin no longer has', async () => {
      await installApproved()
      await seed('package:greeter', { '*': 'on', 'skill:hello': 'off', 'skill:wave': 'off', 'subagent:helper': 'off' })
      await seed('package:greeter/mcp:mem', { '*': 'off', recall: 'read' })
      await list('package:greeter/mcp:mem')

      repos.greeter!.commitFiles({ 'skills/hello/SKILL.md': null, '.mcp.json': null })
      await repin()
      expect(await accessRows()).toEqual([
        'package:greeter *=on',
        'package:greeter skill:wave=off',
        'package:greeter subagent:helper=off',
      ])
      expect(await listings()).toEqual([])
    })

    it("drops a one-skill package's root skill rows keyed by its old folder name", async () => {
      repos.solo = gitRepo({
        '.claude-plugin/plugin.json': JSON.stringify({ name: 'solo' }),
        'SKILL.md': '---\ndescription: Alone.\n---\n\nAlone.\n',
      })
      const installed = await store.create(await installer.prepare(validateSource({ kind: 'git', url: 'https://git.test/solo.git' })))
      expect(installed.review.skills).toEqual(['solo:solo'])
      // Before Task 4 the root skill was keyed by the cache folder it was vetted in.
      await seed('package:solo', {
        'skill:solo': 'off',
        'skill:pkg': 'off',
        [`skill:${installed.commit_sha}-${'0123456789abcdef'}`]: 'off',
      })
      await store.approve('solo', installed.commit_sha, installed.content_hash)
      expect(await accessRows()).toEqual(['package:solo skill:solo=off'])
    })

    it("keeps a local server's rows while the pin, approved with allow_refused, still declares it", async () => {
      repos.greeter!.commitFiles({
        '.mcp.json': JSON.stringify({ mcpServers: { mem: { type: 'http', url: 'https://mcp.example/mcp/' }, box: { command: 'box' } } }),
      })
      const installed = await install()
      expect(installed.review.refused).toEqual([expect.stringMatching(/MCP server "box" is a local \(stdio\) server$/)])
      await seed('package:greeter/mcp:box', { '*': 'off' })
      await seed('package:greeter/mcp:mem', { recall: 'read' })
      await store.approve('greeter', installed.commit_sha, installed.content_hash, true)
      expect(await accessRows()).toEqual(['package:greeter/mcp:box *=off', 'package:greeter/mcp:mem recall=read'])

      repos.greeter!.commitFiles({ '.mcp.json': JSON.stringify({ mcpServers: { mem: { type: 'http', url: 'https://mcp.example/mcp/' } } }) })
      await repin()
      expect(await accessRows()).toEqual(['package:greeter/mcp:mem recall=read'])
    })

    it('a re-pin to a commit with a name past the alias bound fails before any row changes', async () => {
      await installApproved()
      await seed('package:greeter', { 'skill:hello': 'off', 'skill:gone': 'off' })
      const before = await accessRows()
      repos.greeter!.commitFiles({ 'agents/long.md': `---\nname: ${'a'.repeat(1025)}\ndescription: Long.\n---\n\nLong.\n` })
      const current = await store.pinOf('greeter')
      await expect(installer.prepare({ ...current!.source, ref: 'main' })).rejects.toThrow(/longer than 1024/)
      expect(await accessRows()).toEqual(before)
    })

    it("removing a package forgets its items, its servers' rows and their listings", async () => {
      const view = await installApproved()
      await seed('package:greeter', { '*': 'on', 'skill:hello': 'off', [hookRow(view)]: 'off' })
      await seed('package:greeter/mcp:mem', { '*': 'off', recall: 'read' })
      await list('package:greeter/mcp:mem')
      await seed('package:other', { 'skill:x': 'off' })
      await seed('package:other/mcp:mem', { recall: 'read' })
      await list('package:other/mcp:mem')

      expect(await store.delete('greeter')).toBe(true)
      expect(await accessRows()).toEqual(['package:other skill:x=off', 'package:other/mcp:mem recall=read'])
      expect(await listings()).toEqual(['package:other/mcp:mem'])
    })

    it('a re-install starts at the defaults', async () => {
      // What an older replica's remove left behind.
      await seed('package:greeter', { '*': 'off', 'skill:hello': 'off' })
      await seed('package:greeter/mcp:mem', { '*': 'off' })
      await list('package:greeter')
      await list('package:greeter/mcp:mem')
      await install()
      expect(await accessRows()).toEqual([])
      expect(await listings()).toEqual([])
      // A refused second install forgets nothing.
      await seed('package:greeter', { 'skill:hello': 'off' })
      await expect(install()).rejects.toMatchObject({ status: 409 })
      expect(await accessRows()).toEqual(['package:greeter skill:hello=off'])
    })

    it('a package named a_b keeps its rows when a.b is removed', async () => {
      const plugin = (name: string) => ({
        '.claude-plugin/plugin.json': JSON.stringify({ name }),
        'skills/s/SKILL.md': '---\ndescription: S.\n---\n\nS.\n',
      })
      repos.dot = gitRepo(plugin('a.b'))
      repos.under = gitRepo(plugin('a_b'))
      await store.create(await installer.prepare(validateSource({ kind: 'git', url: 'https://git.test/dot.git' })))
      await store.create(await installer.prepare(validateSource({ kind: 'git', url: 'https://git.test/under.git' })))
      for (const name of ['a.b', 'a_b', 'aXb', 'a.b.c']) {
        await seed(`package:${name}`, { 'skill:s': 'off' })
        await seed(`package:${name}/mcp:m`, { t: 'read' })
        await list(`package:${name}/mcp:m`)
      }

      expect(await store.delete('a.b')).toBe(true)
      expect(await accessRows()).toEqual([
        'package:a.b.c skill:s=off',
        'package:a.b.c/mcp:m t=read',
        'package:aXb skill:s=off',
        'package:aXb/mcp:m t=read',
        'package:a_b skill:s=off',
        'package:a_b/mcp:m t=read',
      ])
      expect(await store.delete('a_b')).toBe(true)
      expect(await accessRows()).toEqual([
        'package:a.b.c skill:s=off',
        'package:a.b.c/mcp:m t=read',
        'package:aXb skill:s=off',
        'package:aXb/mcp:m t=read',
      ])
      expect(await listings()).toEqual(['package:a.b.c/mcp:m', 'package:aXb/mcp:m'])
    })

    it('a pending review without hook ids keeps the hook overrides', async () => {
      const first = await installApproved()
      await seed('package:greeter', { [hookRow(first)]: 'off' })
      repos.greeter!.commitFiles({
        'hooks/hooks.json': JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'prompt', prompt: 'Was the user waved at?' }] }] } }),
      })
      const current = await store.pinOf('greeter')
      const pending = await store.setPending('greeter', await installer.prepare({ ...current!.source, ref: 'main' }))
      // As an older replica stores it: no hook has an id.
      await db.sql`
        UPDATE ai_plugin_packages
        SET pending_review = jsonb_set(pending_review, '{hooks}',
          (SELECT jsonb_agg(h - 'id') FROM jsonb_array_elements(pending_review -> 'hooks') AS h))`
      await seed('package:greeter', { 'skill:gone': 'off' })
      await store.approve('greeter', pending.pending!.commit_sha, pending.pending!.content_hash)
      // It cannot tell a kept hook from a lost one, nor name items as this code does: no item row goes.
      expect(await accessRows()).toEqual([`package:greeter ${hookRow(first)}=off`, 'package:greeter skill:gone=off'])
    })

    it('an off root skill survives a package switch write over an old review', async () => {
      repos.solo = gitRepo({
        '.claude-plugin/plugin.json': JSON.stringify({ name: 'solo' }),
        'SKILL.md': '---\ndescription: Alone.\n---\n\nAlone.\n',
      })
      const installed = await store.create(await installer.prepare(validateSource({ kind: 'git', url: 'https://git.test/solo.git' })))
      await store.approve('solo', installed.commit_sha, installed.content_hash)
      await seed('package:solo', { 'skill:solo': 'off' })
      // The review as an older vet wrote it: the root skill named by its vet folder, no `described`.
      await db.sql`
        UPDATE ai_plugin_packages
        SET review = (review - 'described' - 'local_servers') || jsonb_build_object('skills', jsonb_build_array('solo:pkg'))`
      await store.setEnabled('solo', true)
      expect(await accessRows()).toEqual(['package:solo skill:solo=off'])
    })

    it("keeps every server's rows over a review stored without its server list", async () => {
      await installApproved()
      await seed('package:greeter/mcp:mem', { recall: 'read' })
      await seed('package:greeter/mcp:box', { '*': 'off' })
      await list('package:greeter/mcp:box')
      await db.sql`UPDATE ai_plugin_packages SET review = review - 'local_servers'`
      await store.setEnabled('greeter', true)
      expect(await accessRows()).toEqual(['package:greeter/mcp:box *=off', 'package:greeter/mcp:mem recall=read'])
      expect(await listings()).toEqual(['package:greeter/mcp:box'])
    })

    it('keeps the rows of a local server whose name holds a newline', async () => {
      repos.greeter!.commitFiles({
        '.mcp.json': JSON.stringify({ mcpServers: { mem: { type: 'http', url: 'https://mcp.example/mcp/' }, 'a\nb': { command: 'box' } } }),
      })
      const installed = await install()
      expect(installed.review.local_servers).toEqual(['a\nb'])
      await seed('package:greeter/mcp:a\nb', { '*': 'off' })
      await store.approve('greeter', installed.commit_sha, installed.content_hash, true)
      expect(await accessRows()).toEqual(['package:greeter/mcp:a\nb *=off'])
    })

    it("rows an older replica's approval left are pruned by the next write to the package", async () => {
      const view = await installApproved()
      await seed('package:greeter', {
        'skill:hello': 'off',
        'skill:gone': 'off',
        'subagent:gone': 'off',
        'builtin:Bash': 'off',
        [hookRow(view)]: 'off',
        'hook:Stop:0000000000000000': 'off',
      })
      await seed('package:greeter/mcp:mem', { recall: 'read' })
      await seed('package:greeter/mcp:old', { '*': 'off' })
      await list('package:greeter/mcp:old')

      await store.setEnabled('greeter', true)
      expect(await accessRows()).toEqual([
        `package:greeter ${hookRow(view)}=off`,
        'package:greeter skill:hello=off',
        'package:greeter/mcp:mem recall=read',
      ])
      expect(await listings()).toEqual([])

      // setPending and discardPending prune on the current review too.
      await seed('package:greeter', { 'skill:gone': 'off' })
      repos.greeter!.commitFiles({ 'README.md': 'v2\n' })
      await store.setPending('greeter', await installer.prepare({ ...(await store.pinOf('greeter'))!.source, ref: 'main' }))
      expect(await accessRows()).not.toContain('package:greeter skill:gone=off')
      await seed('package:greeter', { 'skill:gone': 'off' })
      await store.discardPending('greeter')
      expect(await accessRows()).not.toContain('package:greeter skill:gone=off')
      // And so does approving the current pin again.
      await seed('package:greeter', { 'skill:gone': 'off' })
      await store.approve('greeter', view.commit_sha, view.content_hash)
      expect(await accessRows()).not.toContain('package:greeter skill:gone=off')
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

      // The old PATCH switch is gone (#4828): the capabilities API switches a package.
      expect((await a.request('/api/v1/ai/plugin-packages/greeter', json('PATCH', { enabled: true }))).status).toBe(404)
      const approve = await a.request(
        '/api/v1/ai/plugin-packages/greeter/approve',
        json('POST', { commit_sha: installed.commit_sha, content_hash: installed.content_hash }),
      )
      expect(approve.status).toBe(200)
      expect((await a.request('/api/v1/ai/plugin-packages/greeter', json('PATCH', { enabled: true }))).status).toBe(404)
      expect((await store.get('greeter'))?.enabled).toBe(false)
      await store.setEnabled('greeter', true)
      const enabled = await a.request('/api/v1/ai/plugin-packages/greeter', { headers: READ })
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

    it("serves a pending re-pin's review stored without hook ids with the verified copy's ids (#4757)", async () => {
      const installed = await install()
      await store.approve('greeter', installed.commit_sha, installed.content_hash)
      repos.greeter!.commitFiles({
        'hooks/hooks.json': JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'prompt', prompt: 'Was the user waved at?' }] }] } }),
      })
      const current = await store.pinOf('greeter')
      await store.setPending('greeter', await installer.prepare({ ...current!.source, ref: 'main' }))
      const [row] = await db.sql<{ id: string }[]>`SELECT pending_review -> 'hooks' -> 0 ->> 'id' AS id FROM ai_plugin_packages`
      const id = row!.id
      expect(id).toMatch(/^[0-9a-f]{16}$/)
      // As an older replica stores it: no hook has an id.
      await db.sql`
        UPDATE ai_plugin_packages
        SET pending_review = jsonb_set(pending_review, '{hooks}',
          (SELECT jsonb_agg(h - 'id') FROM jsonb_array_elements(pending_review -> 'hooks') AS h))`
      const ids = async (a: ReturnType<typeof app>) => {
        const one = (await (await a.request('/api/v1/ai/plugin-packages/greeter', { headers: READ })).json()) as PackageView
        const listed = (await (await a.request('/api/v1/ai/plugin-packages', { headers: READ })).json()) as PackageView[]
        return [one, listed.find((p) => p.name === 'greeter')!].map((v) => v.pending!.review.hooks.map((h) => h.id))
      }
      // The current pin's copy, as a turn leaves it.
      const pin = (await store.pinOf('greeter'))!
      ;(await installer.acquire(pin)).release()
      const currentDir = installer.cacheDir(pin)
      const cached = () => readdirSync(path.join(cacheRoot, 'greeter')).sort()
      const before = cached()
      expect(await ids(app())).toEqual([[undefined], [undefined]])
      const verified = createApp({ ...appDeps(), verifiedPackageReview: (p) => verifiedReview(installer, p) })
      // No copy of the re-pin is cached: the GET fetches none, and answers the review as stored.
      expect(await ids(verified)).toEqual([[undefined], [undefined]])
      expect(cached()).toEqual(before)
      // Once the review's file reads have cached it, its ids are read from it, and no GET moves the cache.
      const file = await verified.request('/api/v1/ai/plugin-packages/greeter/file?path=hooks%2Fhooks.json&pending=true', { headers: READ })
      expect(file.status).toBe(200)
      const after = cached()
      expect(await ids(verified)).toEqual([[id], [id]])
      expect(cached()).toEqual(after)
      expect(existsSync(currentDir)).toBe(true)
    })

    it('lists the built-in plugins first; they switch through ai_access, and are never removed', async () => {
      const access = new AccessStore(db.sql)
      const a = createApp({ ...appDeps(), access })
      const listed = (await (await a.request('/api/v1/ai/plugin-packages', { headers: READ })).json()) as {
        name: string
        built_in?: boolean
        enabled: boolean
        review: { skills: string[] }
      }[]
      expect(listed.map((p) => [p.name, p.built_in, p.enabled])).toEqual([
        ['scadbuddy', true, true],
      ])
      expect(listed[0]!.review.skills).toContain('scadbuddy:authoring')

      // The old PATCH switch is gone (#4828): the switch is the ai_access row (/capabilities writes it).
      const own = () => access.rows([BUILTIN_SOURCES.ownPlugin])
      const off = await a.request('/api/v1/ai/plugin-packages/scadbuddy', json('PATCH', { enabled: false }))
      expect(off.status).toBe(404)
      expect(await own()).toEqual([])
      await access.setEnabled(BUILTIN_SOURCES.ownPlugin, false)
      expect((await own()).map((r) => [r.item, r.access])).toEqual([['*', 'off']])
      expect(await ownPluginEnabled(access)).toBe(false)
      expect(await (await a.request('/api/v1/ai/plugin-packages/scadbuddy', { headers: READ })).json()).toMatchObject({ enabled: false })
      await access.setEnabled(BUILTIN_SOURCES.ownPlugin, true)
      expect((await own()).map((r) => [r.item, r.access])).toEqual([['*', 'on']])
      const one = await a.request('/api/v1/ai/plugin-packages/scadbuddy', { headers: READ })
      expect(await one.json()).toMatchObject({ name: 'scadbuddy', built_in: true, enabled: true })

      for (const [method, url, body] of [
        ['DELETE', '/api/v1/ai/plugin-packages/scadbuddy', undefined],
        ['POST', '/api/v1/ai/plugin-packages/scadbuddy/approve', { commit_sha: 'a', content_hash: 'b' }],
        ['POST', '/api/v1/ai/plugin-packages/scadbuddy/repin', {}],
        ['DELETE', '/api/v1/ai/plugin-packages/scadbuddy/pending', undefined],
      ] as const) {
        const res = await a.request(url, body === undefined ? { method, headers: UI } : json(method, body))
        expect(res.status, `${method} ${url}`).toBe(409)
        expect(await res.json()).toMatchObject({ built_in: true })
      }
    })

    it('never loads a package stored under a built-in\'s name, and lets DELETE remove it', async () => {
      // Stored before builtins.ts, when a reserved name was allowable.
      const installed = await install()
      await store.approve('greeter', installed.commit_sha, installed.content_hash)
      await store.setEnabled('greeter', true)
      await db.sql`UPDATE ai_plugin_packages SET name = 'scadbuddy' WHERE name = 'greeter'`
      const loaded = await loadPackagesForRun(store, installer)
      expect(loaded.paths).toEqual([])
      expect(loaded.problems).toEqual([expect.stringMatching(/scadbuddy was not loaded: "scadbuddy" is built in/)])

      const a = createApp({ ...appDeps(), access: new AccessStore(db.sql) })
      const listed = (await (await a.request('/api/v1/ai/plugin-packages', { headers: READ })).json()) as {
        name: string
        built_in?: boolean
      }[]
      expect(listed.map((p) => [p.name, p.built_in ?? false])).toEqual([
        ['scadbuddy', true],
        ['scadbuddy', false],
      ])
      const del = () => a.request('/api/v1/ai/plugin-packages/scadbuddy', { method: 'DELETE', headers: UI })
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
        too_large: false,
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
      const a = createApp({ ...appDeps(), access: new AccessStore(db.sql) })
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

    it('answers 422 for a re-pin to a commit no approval can allow, and changes no access row', async () => {
      const installed = await install()
      await store.approve('greeter', installed.commit_sha, installed.content_hash)
      await db.sql`INSERT INTO ai_access (source, item, access, updated_by) VALUES ('package:greeter', 'skill:gone', 'off', 'test')`
      repos.greeter!.commitFiles({ 'agents/long.md': `---\nname: ${'a'.repeat(1025)}\ndescription: Long.\n---\n\nLong.\n` })
      const res = await app().request('/api/v1/ai/plugin-packages/greeter/repin', json('POST', { ref: 'main' }))
      expect(res.status).toBe(422)
      expect(((await res.json()) as { problems: string[] }).problems).toContainEqual(expect.stringMatching(/longer than 1024/))
      expect((await store.get('greeter'))?.pending).toBeNull()
      const rows = await db.sql<{ item: string }[]>`SELECT item FROM ai_access WHERE source = 'package:greeter'`
      expect(rows.map((r) => r.item)).toEqual(['skill:gone'])
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
    // sessions/manager.ts: a package that declares an http MCP server is loaded as its copy without it.
    // The copy without its http servers, at `<hex>/<package name>` by its own hash, not the pin's (#4757).
    const expectStrippedCopy = async (paths: readonly string[] | undefined, stateDir: string, pin: string) => {
      expect(paths).toHaveLength(1)
      const copy = paths![0]!
      const hex = path.basename(path.dirname(copy))
      expect(path.dirname(path.dirname(copy))).toBe(path.join(stateDir, 'plugins-local'))
      expect(path.basename(copy)).toBe('greeter')
      expect(`sha256:${hex}`).not.toBe(pin)
      expect((await hashTree(copy)).hash).toBe(`sha256:${hex}`)
    }

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
      const paths = await tempPaths()
      const m = manager({
        sql: db.sql,
        paths,
        run: runner,
        packagePlugins: () => loadPackagesForRun(store, installer),
      })
      const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
      await turn!.done
      // The fixture declares an http MCP server: Claude Code gets the copy without it (#2320).
      await expectStrippedCopy(runs[0]?.pluginPaths, paths.stateDir, installed.content_hash)

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
      const access = new AccessStore(db.sql)
      const runs: HarnessRun[] = []
      const scripted = scriptedRunner(() => ({ reply: 'hi' }))
      const m = manager({
        sql: db.sql,
        paths: await tempPaths(),
        access: new AccessResolver(access),
        ownPlugin: '/opt/own-plugin',
        run: (run) => {
          runs.push(run)
          return scripted.runner(run)
        },
      })
      await (await m.start(agentA, { origin: 'mcp', prompt: 'hi' })).turn!.done
      expect(runs[0]?.ownPlugin).toBe('/opt/own-plugin')

      await access.setEnabled(BUILTIN_SOURCES.ownPlugin, false, { actor: UI_ACTOR, surface: 'http' })
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
      const paths = await tempPaths()
      const m = manager({
        sql: db.sql,
        paths,
        run: (run) => {
          runs.push(run)
          return scripted.runner(run)
        },
        packagePlugins: () => loadPackagesForRun(store, installer),
      })
      const { turn } = await m.start(agentA, { origin: 'mcp', prompt: 'hi' })
      await turn!.done
      expect(runs[0]?.pluginPaths).toBeUndefined()
      await expectStrippedCopy(runs[0]?.allowedPluginPaths, paths.stateDir, installed.content_hash)
    })
  })
})
