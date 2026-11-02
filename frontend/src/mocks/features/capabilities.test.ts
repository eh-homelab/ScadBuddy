import { describe, expect, it } from 'vitest'
import { aiPlugins } from '../../api/aiPlugins'
import { GREETER_V1, seedStoredPackage, setListedToolNames, setRemotePluginAccess } from '../aiPlugins'
import { aiCapabilities } from '../../api/capabilities'
import { summarize } from '../../components/capabilities/access'
import { reloadMockState, seedBuiltInRow, setPackageChecking, setPackageProblem } from './capabilities'
import type { ApiError } from '../../api/client'

describe('capabilities mock (#2171)', () => {
  it('seeds ten ScadBuddy tools and the other built-ins', async () => {
    const sources = await aiCapabilities.list()
    expect(sources.map((s) => s.id)).toEqual([
      'builtin:scadbuddy',
      'builtin:browser',
      'builtin:http',
      'builtin:own-plugin',
      'builtin:claude-code',
      'remote:hindsight',
    ])
    expect(sources[0]!.items).toHaveLength(10)
    expect(sources[1]).toMatchObject({ options_route: '/api/v1/ai/settings/browser-origins', items: [{ id: 'browser_click', default: 'write' }] })
    // Claude Code's own built-ins by their own names; Bash is off by default (plan 2026-10-13 Step 9.6).
    expect(sources[4]!.items.find((i) => i.id === 'Bash')).toMatchObject({ harness_name: 'Bash', default: 'off', access: 'off' })
    expect(sources[4]!.items.find((i) => i.id === 'Read')).toMatchObject({ harness_name: 'Read', default: 'read' })
  })

  it('a migrated row at the tool\'s own tier is overridden, counted as a change, and Reset removes it (#4994)', async () => {
    seedBuiltInRow('builtin:scadbuddy', 'list_models', 'read')
    const source = await aiCapabilities.source('builtin:scadbuddy')
    expect(source.items.find((i) => i.id === 'list_models')).toMatchObject({ default: 'read', access: 'read', overridden: true })
    expect(summarize(source)).toBe('10 tools, 1 changed')
    const after = await aiCapabilities.resetItem('builtin:scadbuddy', 'list_models')
    expect(after.items.find((i) => i.id === 'list_models')).toMatchObject({ access: 'read', overridden: false })
    expect(summarize(after)).toBe('10 tools')
  })

  it('lists a remote plugin after a refresh, recall at its stored read', async () => {
    expect((await aiCapabilities.source('remote:hindsight')).items.map((i) => [i.id, i.missing])).toEqual([['recall', true]])
    const { source } = await aiCapabilities.refresh('remote:hindsight')
    expect(source.listed_at).not.toBeNull()
    // hindsight is switched off: recall reads off, its own access kept (#4757).
    expect(source.items.find((i) => i.id === 'recall')).toMatchObject({
      access: 'off',
      own_access: 'read',
      reason: 'Off while "hindsight" is switched off',
      overridden: true,
      suggested: 'read',
      missing: false,
    })
    expect(source.items.find((i) => i.id === 'files.list')).toMatchObject({ allowed: ['off', 'outward'] })
    expect(source.items.find((i) => i.id === 'recall')?.label).toBe('Recall memories')
    expect(await aiCapabilities.details('remote:hindsight', 'recall')).toMatchObject({ annotations: { readOnlyHint: true }, omitted: [] })
    // A literal % in an id is one segment's %25, refused as the agent does.
    await expect(aiCapabilities.details('remote:hindsight', '100%')).rejects.toMatchObject({ status: 404 })
  })

  it('writes remote access into the plugins state, and reads plugins registered there', async () => {
    await aiCapabilities.refresh('remote:hindsight')
    await aiCapabilities.setItem('remote:hindsight', 'retain', 'off')
    await aiCapabilities.setEnabled('remote:hindsight', true)
    const [listed] = (await aiPlugins.listPlugins()).filter((p) => p.name === 'hindsight')
    expect(listed).toMatchObject({ enabled: true, disabled_tools: ['retain'] })

    // The default resets.
    const after = await aiCapabilities.setItem('remote:hindsight', 'recall', 'outward')
    expect(after.items.find((i) => i.id === 'recall')).toMatchObject({ access: 'outward', overridden: false })

    await aiPlugins.createRemote({ name: 'notes', url: 'https://notes.example/mcp/' })
    expect(await aiCapabilities.source('remote:notes')).toMatchObject({ items: [], listed_at: null })
    await aiPlugins.deleteRemote('notes')
    await expect(aiCapabilities.source('remote:notes')).rejects.toMatchObject({ status: 404 })
  })

  it('a refresh answers no tools beside the source, as the agent does (final review M3)', async () => {
    const { test, source } = await aiCapabilities.refresh('remote:hindsight')
    expect(test).not.toHaveProperty('tools')
    expect(source.items.length).toBeGreaterThan(0)
  })

  it('a name listed twice is one row, fixed off, as the agent marks it (final review I1)', async () => {
    setListedToolNames(['recall', 'recall', 'retain'])
    const { source } = await aiCapabilities.refresh('remote:hindsight')
    expect(source.items.map((i) => i.id)).toEqual(['recall', 'retain'])
    expect(source.items[0]).toMatchObject({ access: 'off', fixed: { access: 'off', reason: 'Listed more than once under this name, so it is hidden' } })
  })

  it('three tools on one harness name say all of them, naming only the others, as the agent does', async () => {
    setListedToolNames(['c.d', 'c_d', 'c d'])
    const { source } = await aiCapabilities.refresh('remote:hindsight')
    expect(source.items.find((i) => i.id === 'c.d')?.fixed?.reason).toBe('Shares its name with c_d, c d, so all of them are hidden')
  })

  it("a tool off through a stored entry of another name says which to reset, as the agent does (final review I2)", async () => {
    setListedToolNames(['a_b'])
    await aiPlugins.createRemote({ name: 'notes', url: 'https://notes.example/mcp/' })
    setRemotePluginAccess('notes', { disabled_tools: ['a.b'] })
    const { source } = await aiCapabilities.refresh('remote:notes')
    expect(source.items.find((i) => i.id === 'a_b')).toMatchObject({
      access: 'off',
      overridden: true,
      reason: 'Off because "a.b" is off, which Claude Code names the same: reset "a.b" to change it',
    })
  })

  it('refuses as the agent does', async () => {
    await aiCapabilities.refresh('remote:hindsight')
    const refusal = (run: Promise<unknown>) => run.catch((e: unknown) => (e as ApiError).status)
    expect(await refusal(aiCapabilities.setItem('remote:hindsight', 'files.list', 'read'))).toBe(409)
    expect(await refusal(aiCapabilities.setItem('builtin:scadbuddy', 'list_models', 'on'))).toBe(400)
    expect(await refusal(aiCapabilities.setItem('builtin:scadbuddy', 'nope', 'off'))).toBe(404)
    expect(await refusal(aiCapabilities.refresh('builtin:http'))).toBe(409)
    const http = await aiCapabilities.setItem('builtin:http', 'http_request', 'read')
    expect(http.items[0]).toMatchObject({ access: 'read', overridden: true })
    expect((await aiCapabilities.resetItem('builtin:http', 'http_request')).items[0]).toMatchObject({ access: null, overridden: false })
  })

  describe('package sources (#4757)', () => {
    const MEM = { name: 'mem', type: 'http', url: 'https://mcp.example/mcp/' }
    const source = (id: string) => aiCapabilities.source(id)
    const refused = (run: Promise<unknown>) => run.then(() => undefined, (e: unknown) => ({ status: (e as ApiError).status, detail: (e as ApiError).detail }))
    /** The first read of a new pin answers checking; this one settles it. */
    const settled = async (id: string) => {
      await source(id)
      return source(id)
    }

    it('keeps an installed package and its rows across a reload', async () => {
      await aiPlugins.installPackage({ kind: 'git', url: 'https://git.example/greeter.git' })
      await aiPlugins.approvePackage('greeter', GREETER_V1.commit, GREETER_V1.hash)
      await aiCapabilities.setEnabled('package:greeter', true)
      await settled('package:greeter')
      await aiCapabilities.setItem('package:greeter', 'skill:hello', 'off')

      reloadMockState()

      expect((await aiPlugins.listPackages()).find((p) => p.name === 'greeter')).toMatchObject({ approved: true, enabled: true })
      const after = await source('package:greeter')
      expect(after.items.find((i) => i.id === 'skill:hello')).toMatchObject({ access: 'off', own_access: 'off', overridden: true })
      expect(after.checking).toBe(false)
    })

    it("answers a new pin's first read checking, as the agent's load check does, and the next one settles", async () => {
      seedStoredPackage('greeter')
      const first = await source('package:greeter')
      expect(first).toMatchObject({ enabled: true, checking: true, problem: null })
      expect(first.items.find((i) => i.id === 'skill:hello')).toMatchObject({
        access: 'off',
        own_access: 'on',
        reason: 'Checking whether "greeter" can load…',
        fixed: null,
      })
      expect(first.children[0]).toMatchObject({ id: 'package:greeter/mcp:mem', checking: true })
      const second = await source('package:greeter')
      expect(second.checking).toBe(false)
      expect(second.items.find((i) => i.id === 'skill:hello')).toMatchObject({ access: 'on', reason: null })

      setPackageChecking('greeter', true)
      expect((await source('package:greeter')).checking).toBe(true)
      expect(await refused(aiCapabilities.refresh('package:greeter/mcp:mem'))).toEqual({
        status: 409,
        detail: 'Checking whether "greeter" can load… Try again shortly',
      })
      setPackageChecking('greeter', false)
      expect((await source('package:greeter')).checking).toBe(false)
    })

    it('a package refused at load keeps its row on, reads not enabled with the problem, and still switches off', async () => {
      seedStoredPackage('greeter')
      const problem = 'plugin package greeter is not loaded: skills/hello/SKILL.md changed since it was approved'
      setPackageProblem('greeter', problem)
      const view = await source('package:greeter')
      expect(view).toMatchObject({ enabled: false, switchable: true, problem, checking: false })
      expect(view.items.every((i) => i.access === 'off' && i.own_access === 'off' && i.fixed?.reason === problem && i.reason === problem)).toBe(true)
      expect(view.children[0]).toMatchObject({ problem })
      expect((await aiPlugins.listPackages()).find((p) => p.name === 'greeter')?.enabled).toBe(true)
      expect(await refused(aiCapabilities.refresh('package:greeter/mcp:mem'))).toEqual({ status: 409, detail: problem })

      await aiCapabilities.setEnabled('package:greeter', false)
      expect((await aiPlugins.listPackages()).find((p) => p.name === 'greeter')?.enabled).toBe(false)
    })

    it('servers whose names a turn carries alike are fixed off, both, with the reason', async () => {
      seedStoredPackage('greet.er', { review: { mcp_servers: [MEM] } })
      seedStoredPackage('greet_er', { review: { mcp_servers: [MEM] } })
      seedBuiltInRow('package:greet.er/mcp:mem', 'recall', 'read')
      const view = (await settled('package:greet.er')).children[0]!
      // Each package's own first read settles only its own check.
      await settled('package:greet_er')
      const reason = 'Its server is named plugin_greet_er_mem, as "mem" of plugin package greet_er is, so none of them is offered'
      expect(view.items).toEqual([expect.objectContaining({ id: 'recall', missing: true, access: 'off', own_access: 'off', fixed: { access: 'off', reason }, reason })])
      expect(await refused(aiCapabilities.refresh('package:greet.er/mcp:mem'))).toEqual({ status: 409, detail: reason })
      expect(await refused(aiCapabilities.refresh('package:greet_er/mcp:mem'))).toEqual({
        status: 409,
        detail: 'Its server is named plugin_greet_er_mem, as "mem" of plugin package greet.er is, so none of them is offered',
      })

      // A package switched off is not loaded, so the other's name is its own again.
      await aiCapabilities.setEnabled('package:greet_er', false)
      const { source: listed } = await aiCapabilities.refresh('package:greet.er/mcp:mem')
      expect(listed.items.find((i) => i.id === 'recall')).toMatchObject({ access: 'read', fixed: null, harness_name: 'mcp__plugin_greet_er_mem__recall' })
    })

    it("a read of one package leaves another's first-read checking alone", async () => {
      seedStoredPackage('greeter')
      seedStoredPackage('other')
      await source('package:greeter')
      // `other`'s servers were counted for greeter's collisions, but no read showed `other` yet.
      expect((await aiCapabilities.list()).find((s) => s.id === 'package:other')?.checking).toBe(true)
    })

    it('a review from before hook ids lists nothing until its copy is cached, and reads checking while on', async () => {
      seedStoredPackage('greeter', { review: { hooks: [{ event: 'Stop', type: 'prompt' }], described: undefined } })
      const on = await settled('package:greeter')
      expect(on).toMatchObject({ enabled: true, checking: true, items: [], children: [] })
      await aiCapabilities.setEnabled('package:greeter', false)
      expect(await source('package:greeter')).toMatchObject({ enabled: false, checking: false, items: [], children: [] })
    })

    it('a server whose name would hold __ is not offered', async () => {
      seedStoredPackage('odd', { review: { mcp_servers: [{ ...MEM, name: '_x' }] } })
      await settled('package:odd')
      expect(await refused(aiCapabilities.refresh('package:odd/mcp:_x'))).toEqual({
        status: 409,
        detail: 'plugin package odd: MCP server "_x" has a name that cannot be offered',
      })
    })

    it("an allowed package's local servers are children whose tools are never listed here", async () => {
      seedStoredPackage('local', { review: { mcp_servers: [], local_servers: ['tool'] }, allow_refused: true })
      seedStoredPackage('strict', { review: { mcp_servers: [], local_servers: ['tool'] } })
      expect((await settled('package:local')).children.map((c) => [c.id, c.enabled, c.switchable])).toEqual([['package:local/mcp:tool', true, true]])
      expect((await settled('package:strict')).children).toEqual([])
      expect(await refused(aiCapabilities.refresh('package:local/mcp:tool'))).toEqual({
        status: 409,
        detail: '"package:local/mcp:tool" is started only by a turn, so its tools are not listed here',
      })
      const off = await aiCapabilities.setEnabled('package:local/mcp:tool', false)
      expect(off).toMatchObject({ id: 'package:local/mcp:tool', enabled: false })
    })
  })
})
