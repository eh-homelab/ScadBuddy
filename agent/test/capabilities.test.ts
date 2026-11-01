import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import type { AuditContext } from '../src/audit/log.js'
import { Capabilities, CapabilityError, type CapabilityProvider, type ItemView, type SourceView } from '../src/access/capabilities.js'
import { BUILTIN_SOURCES, MIGRATED_BY } from '../src/access/model.js'
import { builtInProviders, ownPluginItems } from '../src/access/providers.js'
import { HTTP_TOOL_DESCRIPTION } from '../src/harness/httpRequest.js'
import { BUILT_INS, reviewOf } from '../src/plugins/packages/builtins.js'
import { BROWSER_ORIGINS_PATH } from '../src/routes/browserOrigins.js'
import { listExternal } from '../src/tools/manifest.js'
import { createExternalServer } from '../src/tools/projections.js'
import { defineTool, json, type Tool } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { memoryAccess } from './support/memoryAccess.js'

// The capabilities tree over the four built-in sources (#2171, Task 3).

const stand = (name: string, risk: 'read' | 'write' | 'outward'): Tool =>
  defineTool({ name, label: `Label ${name}`, description: `does ${name}`, input: z.object({ id: z.string() }), risk, routes: [], handler: async () => json({}) })

const TOOLS: readonly Tool[] = [stand('get_x', 'read'), stand('put_x', 'write'), stand('send_x', 'outward'), stand('browser_click', 'write')]

const ctx: AuditContext = { actor: { kind: 'browser', id: 'browser', label: 'Browser' }, surface: 'http' }

function setup(rows: Parameters<typeof memoryAccess>[0] = []) {
  const mem = memoryAccess(rows)
  const caps = new Capabilities([builtInProviders({ access: mem.access, tools: TOOLS })])
  return { ...mem, caps }
}

const item = (source: SourceView, id: string) => source.items.find((i) => i.id === id)!

async function status(p: Promise<unknown>): Promise<number> {
  try {
    await p
  } catch (err) {
    if (err instanceof CapabilityError) return err.status
    throw err
  }
  return 0
}

function fakeProvider(sources: readonly SourceView[]): CapabilityProvider {
  const all = sources.flatMap((s) => [s, ...s.children])
  return {
    owns: (id) => all.some((s) => s.id === id) || id.startsWith('package:'),
    list: async () => [...sources],
    get: async (id) => all.find((s) => s.id === id),
    setEnabled: async () => {},
    setItem: async () => {},
    resetItem: async () => {},
    details: async (id, i) => {
      const found = all.find((s) => s.id === id)?.items.find((x) => x.id === i)
      return found && { item: found, input_schema: null, output_schema: null, annotations: null, raw: null, omitted: [] }
    },
  }
}

/** A package `p` (on or off) with one server child `s`, whose tool `grep` is read. */
function parentAndChild(enabled: boolean) {
  const grep: ItemView = { id: 'grep', kind: 'tool', name: 'grep', harness_name: 'mcp__plugin_p_s__grep', label: 'grep', description: null, default: 'outward', default_note: null, suggested: null, access: 'read', own_access: 'read', overridden: true, missing: false, allowed: null, fixed: null, reason: null }
  const child: SourceView = { id: 'package:p/mcp:s', kind: 'package_server', title: 's', enabled: true, switchable: true, options_route: null, listed_at: null, truncated: false, problem: null, checking: false, items: [grep], children: [] }
  const parent: SourceView = { id: 'package:p', kind: 'package', title: 'p', enabled, switchable: true, options_route: null, listed_at: null, truncated: false, problem: null, checking: false, items: [], children: [child] }
  const fake = fakeProvider([parent])
  const setItem = vi.fn(async () => {})
  const provider: CapabilityProvider = {
    ...fake,
    // The child alone, as a provider answers it: its own switch, nothing of its parent's.
    get: async (id) => (id === 'package:p' ? parent : id === 'package:p/mcp:s' ? child : undefined),
    parentOf: (id) => (id.startsWith('package:p/mcp:') ? 'package:p' : undefined),
    setItem,
    refresh: async () => ({ test: {} as never, source: child }),
  }
  return { caps: new Capabilities([provider]), setItem }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the capabilities tree', () => {
  it('lists the five built-in sources in order, each tool at its declared tier', async () => {
    const { caps } = setup()
    const tree = await caps.tree()
    expect(tree.map((s) => s.id)).toEqual([
      BUILTIN_SOURCES.scadbuddy,
      BUILTIN_SOURCES.browser,
      BUILTIN_SOURCES.http,
      BUILTIN_SOURCES.ownPlugin,
      BUILTIN_SOURCES.claudeCode,
    ])
    expect(tree.map((s) => s.title)).toEqual([
      'ScadBuddy tools',
      'Your browser tab',
      'HTTP requests',
      'ScadBuddy skills and subagents',
      "Claude Code's built-in tools",
    ])
    expect(tree.map((s) => s.switchable)).toEqual([false, true, true, true, true])
    // Claude Code's own (plan 2026-10-13 Step 9.6): by their own names, Bash off by default.
    const native = tree[4]!.items
    expect(native.find((i) => i.id === 'Read')).toMatchObject({ harness_name: 'Read', default: 'read', access: 'read' })
    expect(native.find((i) => i.id === 'Write')).toMatchObject({ default: 'write', access: 'write' })
    expect(native.find((i) => i.id === 'Bash')).toMatchObject({ default: 'off', access: 'off', default_note: expect.stringContaining('#779') })
    expect(tree.every((s) => s.enabled && s.kind === 'builtin' && s.children.length === 0)).toBe(true)
    const own = tree[0]!
    expect(own.items.map((i) => [i.id, i.default, i.access, i.overridden])).toEqual([
      ['get_x', 'read', 'read', false],
      ['put_x', 'write', 'write', false],
      ['send_x', 'outward', 'outward', false],
    ])
    expect(item(own, 'send_x')).toMatchObject({
      kind: 'tool',
      name: 'send_x',
      harness_name: 'mcp__scadbuddy__send_x',
      label: 'Label send_x',
      description: 'does send_x',
      default_note: null,
      missing: false,
      allowed: null,
      fixed: null,
    })
    expect(tree[3]!.items.map((i) => i.id)).toEqual([
      'skill:authoring',
      'skill:customize',
      'skill:print',
      'subagent:model-author',
      'subagent:print-analyst',
    ])
    expect(tree.flatMap((s) => s.items).every((i) => i.own_access === i.access)).toBe(true)
  })

  it('a lowered outward tool reads back lowered, overridden', async () => {
    const { caps, rows } = setup()
    const source = await caps.setItem(BUILTIN_SOURCES.scadbuddy, 'send_x', 'write', ctx)
    expect(item(source, 'send_x')).toMatchObject({ access: 'write', default: 'outward', overridden: true })
    expect(rows).toEqual([{ source: BUILTIN_SOURCES.scadbuddy, item: 'send_x', access: 'write', updated_by: 'browser:browser' }])
  })

  it('a raised read tool reads back raised', async () => {
    const { caps } = setup()
    const source = await caps.setItem(BUILTIN_SOURCES.scadbuddy, 'get_x', 'outward', ctx)
    expect(item(source, 'get_x')).toMatchObject({ access: 'outward', default: 'read', overridden: true })
  })

  it('off is stored and shown as off', async () => {
    const { caps, rows } = setup()
    const source = await caps.setItem(BUILTIN_SOURCES.scadbuddy, 'put_x', 'off', ctx)
    expect(item(source, 'put_x')).toMatchObject({ access: 'off', overridden: true })
    expect(rows.map((r) => r.access)).toEqual(['off'])
  })

  it('setting an item to its default deletes its row', async () => {
    const { caps, rows } = setup()
    await caps.setItem(BUILTIN_SOURCES.scadbuddy, 'put_x', 'off', ctx)
    const source = await caps.setItem(BUILTIN_SOURCES.scadbuddy, 'put_x', 'write', ctx)
    expect(rows).toEqual([])
    expect(item(source, 'put_x')).toMatchObject({ access: 'write', overridden: false })
  })

  it('resetItem deletes the row', async () => {
    const { caps, rows } = setup()
    await caps.setItem(BUILTIN_SOURCES.scadbuddy, 'send_x', 'read', ctx)
    const source = await caps.resetItem(BUILTIN_SOURCES.scadbuddy, 'send_x', ctx)
    expect(rows).toEqual([])
    expect(item(source, 'send_x')).toMatchObject({ access: 'outward', overridden: false })
  })

  it('shows a migrated row that lowers as the default, and choosing the default deletes it', async () => {
    const { caps, rows } = setup([{ source: BUILTIN_SOURCES.scadbuddy, item: 'send_x', access: 'write', updated_by: MIGRATED_BY }])
    expect(item(await caps.source(BUILTIN_SOURCES.scadbuddy), 'send_x')).toMatchObject({ access: 'outward', overridden: true })
    await caps.setItem(BUILTIN_SOURCES.scadbuddy, 'send_x', 'outward', ctx)
    expect(rows).toEqual([])
  })

  it('a migrated row equal to the tier is overridden, so Reset can remove it (#4994)', async () => {
    const { caps, rows } = setup([{ source: BUILTIN_SOURCES.scadbuddy, item: 'put_x', access: 'write', updated_by: MIGRATED_BY }])
    expect(item(await caps.source(BUILTIN_SOURCES.scadbuddy), 'put_x')).toMatchObject({ access: 'write', default: 'write', overridden: true })
    const source = await caps.resetItem(BUILTIN_SOURCES.scadbuddy, 'put_x', ctx)
    expect(rows).toEqual([])
    expect(item(source, 'put_x')).toMatchObject({ access: 'write', overridden: false })
  })

  it('browser tools live in builtin:browser, with the origins route', async () => {
    const { caps } = setup()
    const browser = await caps.source(BUILTIN_SOURCES.browser)
    expect(browser.options_route).toBe(BROWSER_ORIGINS_PATH)
    expect(browser.items.map((i) => i.id)).toEqual(['browser_click'])
    expect(item(browser, 'browser_click')).toMatchObject({ harness_name: 'mcp__scadbuddy__browser_click', access: 'write' })
    const source = await caps.setItem(BUILTIN_SOURCES.browser, 'browser_click', 'off', ctx)
    expect(item(source, 'browser_click').access).toBe('off')
  })

  it('http_request has a by-call default; any tier is stored', async () => {
    const { caps, rows } = setup()
    const http = await caps.source(BUILTIN_SOURCES.http)
    expect(http.items).toHaveLength(1)
    expect(item(http, 'http_request')).toMatchObject({
      kind: 'tool',
      label: 'Make an HTTP request',
      description: HTTP_TOOL_DESCRIPTION,
      harness_name: 'mcp__scadbuddy_http__http_request',
      default: null,
      access: null,
      overridden: false,
      default_note: 'read for GET and HEAD, outward (asks first) for anything else',
    })
    const set = await caps.setItem(BUILTIN_SOURCES.http, 'http_request', 'outward', ctx)
    expect(rows).toEqual([{ source: BUILTIN_SOURCES.http, item: 'http_request', access: 'outward', updated_by: 'browser:browser' }])
    expect(item(set, 'http_request')).toMatchObject({ access: 'outward', overridden: true })
    const reset = await caps.resetItem(BUILTIN_SOURCES.http, 'http_request', ctx)
    expect(rows).toEqual([])
    expect(item(reset, 'http_request')).toMatchObject({ access: null, overridden: false })
  })

  it('switches builtin:http off and on', async () => {
    const { caps, rows } = setup()
    expect((await caps.setEnabled(BUILTIN_SOURCES.http, false, ctx)).enabled).toBe(false)
    expect(rows).toEqual([{ source: BUILTIN_SOURCES.http, item: '*', access: 'off', updated_by: 'browser:browser' }])
    expect((await caps.setEnabled(BUILTIN_SOURCES.http, true, ctx)).enabled).toBe(true)
  })

  it('a browser tool reads off while builtin:browser is switched off, keeping its own access', async () => {
    const { caps, rows } = setup([{ source: BUILTIN_SOURCES.browser, item: 'browser_click', access: 'read', updated_by: 'browser:browser' }])
    const off = await caps.setEnabled(BUILTIN_SOURCES.browser, false, ctx)
    expect(off.enabled).toBe(false)
    expect(item(off, 'browser_click')).toMatchObject({
      access: 'off',
      own_access: 'read',
      overridden: true,
      reason: 'Off while "Your browser tab" is switched off',
    })
    const [tree] = (await caps.tree()).filter((s) => s.id === BUILTIN_SOURCES.browser)
    expect(item(tree!, 'browser_click')).toMatchObject({ access: 'off', own_access: 'read' })
    // A write answers the source as the tree does.
    const set = await caps.setItem(BUILTIN_SOURCES.browser, 'browser_click', 'outward', ctx)
    expect(item(set, 'browser_click')).toMatchObject({ access: 'off', own_access: 'outward' })
    expect(rows.filter((r) => r.item !== '*').map((r) => r.access)).toEqual(['outward'])
  })

  it('switches builtin:browser off and on', async () => {
    const { caps, rows } = setup()
    const off = await caps.setEnabled(BUILTIN_SOURCES.browser, false, ctx)
    expect(off.enabled).toBe(false)
    // The switch is the `*` row alone: no per-tool off rows.
    expect(rows).toEqual([{ source: BUILTIN_SOURCES.browser, item: '*', access: 'off', updated_by: 'browser:browser' }])
    const on = await caps.setEnabled(BUILTIN_SOURCES.browser, true, ctx)
    expect(on.enabled).toBe(true)
    expect(item(on, 'browser_click')).toMatchObject({ access: 'write', own_access: 'write', reason: null })
  })

  it("lists the own plugin's skills and subagents with their descriptions", async () => {
    const { caps } = setup()
    const own = await caps.source(BUILTIN_SOURCES.ownPlugin)
    const review = reviewOf(BUILT_INS[0]!)
    expect(item(own, 'skill:authoring')).toEqual({
      id: 'skill:authoring',
      kind: 'skill',
      name: 'authoring',
      harness_name: 'scadbuddy:authoring',
      label: 'authoring',
      description: review.described!['skill:authoring'],
      default: 'on',
      default_note: null,
      suggested: null,
      access: 'on',
      own_access: 'on',
      overridden: false,
      missing: false,
      allowed: null,
      fixed: null,
      reason: null,
    })
    expect(item(own, 'subagent:model-author')).toMatchObject({
      kind: 'subagent',
      name: 'model-author',
      harness_name: 'scadbuddy:model-author',
      description: expect.stringContaining('OpenSCAD templates'),
      default: 'on',
      access: 'on',
    })
  })

  it("lists the own plugin's commands as skill items, one per name with its skills", () => {
    const review = { ...reviewOf(BUILT_INS[0]!), skills: ['scadbuddy:a'], commands: ['scadbuddy:a', 'scadbuddy:cmd'], agents: ['scadbuddy:x'], described: { 'skill:a': 'A', 'skill:cmd': 'C' } }
    const items = ownPluginItems(review, [{ source: BUILTIN_SOURCES.ownPlugin, item: 'skill:cmd', access: 'off', updated_by: 'browser:browser' }])
    expect(items.map((i) => [i.id, i.kind, i.harness_name, i.description, i.access])).toEqual([
      ['skill:a', 'skill', 'scadbuddy:a', 'A', 'on'],
      ['skill:cmd', 'skill', 'scadbuddy:cmd', 'C', 'off'],
      ['subagent:x', 'subagent', 'scadbuddy:x', null, 'on'],
    ])
  })

  it('switches an own-plugin skill off and back to its default (the row goes)', async () => {
    const { caps, rows } = setup()
    const off = await caps.setItem(BUILTIN_SOURCES.ownPlugin, 'skill:print', 'off', ctx)
    expect(item(off, 'skill:print')).toMatchObject({ access: 'off', own_access: 'off', overridden: true })
    expect(rows).toEqual([{ source: BUILTIN_SOURCES.ownPlugin, item: 'skill:print', access: 'off', updated_by: 'browser:browser' }])
    const on = await caps.setItem(BUILTIN_SOURCES.ownPlugin, 'skill:print', 'on', ctx)
    expect(rows).toEqual([])
    expect(item(on, 'skill:print')).toMatchObject({ access: 'on', overridden: false })
    // The plugin's switch shows on every item, each keeping its own access.
    const plugin = await caps.setEnabled(BUILTIN_SOURCES.ownPlugin, false, ctx)
    expect(item(plugin, 'subagent:print-analyst')).toMatchObject({
      access: 'off',
      own_access: 'on',
      reason: 'Off while "ScadBuddy skills and subagents" is switched off',
    })
  })

  it('refuses a tier for a skill and on for a tool (400)', async () => {
    const { caps, rows } = setup()
    expect(await status(caps.setItem(BUILTIN_SOURCES.ownPlugin, 'skill:print', 'read', ctx))).toBe(400)
    expect(await status(caps.setItem(BUILTIN_SOURCES.ownPlugin, 'subagent:model-author', 'outward', ctx))).toBe(400)
    expect(await status(caps.setItem(BUILTIN_SOURCES.browser, 'browser_click', 'on', ctx))).toBe(400)
    expect(rows).toEqual([])
  })

  it('builtin:scadbuddy reads enabled whatever a stray * row says', async () => {
    const { caps } = setup([{ source: BUILTIN_SOURCES.scadbuddy, item: '*', access: 'off', updated_by: 'browser:browser' }])
    const own = await caps.source(BUILTIN_SOURCES.scadbuddy)
    expect(own.enabled).toBe(true)
    expect(item(own, 'get_x')).toMatchObject({ access: 'read', reason: null })
  })

  it("an item's own reason that explains off stays; any other gives way to the switch", () => {
    // Through a fake provider: the built-in items carry no reason of their own.
    const base = { kind: 'tool', label: 'x', description: null, default: 'outward', default_note: null, suggested: null, missing: false, allowed: null, fixed: null } as const
    const renamed: ItemView = { ...base, id: 'a.b', name: 'a.b', harness_name: 'mcp__p__a_b', access: 'outward', own_access: 'outward', overridden: false, allowed: ['off', 'outward'], reason: 'renamed' }
    const offAs: ItemView = { ...base, id: 'c_d', name: 'c_d', harness_name: 'mcp__p__c_d', access: 'off', own_access: 'off', overridden: true, reason: 'Off because "c.d" is off' }
    const source: SourceView = { id: 'remote:p', kind: 'remote', title: 'p', enabled: false, switchable: true, options_route: null, listed_at: null, truncated: false, problem: null, checking: false, items: [renamed, offAs], children: [] }
    const caps = new Capabilities([fakeProvider([source])])
    return caps.source('remote:p').then((view) => {
      expect(item(view, 'a.b')).toMatchObject({ access: 'off', own_access: 'outward', reason: 'Off while "p" is switched off', allowed: ['off', 'outward'] })
      expect(item(view, 'c_d')).toMatchObject({ access: 'off', reason: 'Off because "c.d" is off' })
    })
  })

  it('a child of a switched-off parent reads off by its own id, keeping own_access', async () => {
    const { caps, setItem } = parentAndChild(false)
    const child = await caps.source('package:p/mcp:s')
    expect(child.id).toBe('package:p/mcp:s')
    expect(item(child, 'grep')).toMatchObject({ access: 'off', own_access: 'read', reason: 'Off while "p" is switched off' })
    const written = await caps.setItem('package:p/mcp:s', 'grep', 'write', ctx)
    expect(setItem).toHaveBeenCalledTimes(1)
    expect(item(written, 'grep')).toMatchObject({ access: 'off', own_access: 'read' })
    const refreshed = await caps.refresh('package:p/mcp:s')
    expect(item(refreshed.source, 'grep')).toMatchObject({ access: 'off', own_access: 'read' })
    const details = await caps.details('package:p/mcp:s', 'grep')
    expect(details.item).toMatchObject({ access: 'off', own_access: 'read' })
  })

  it('a child of a parent that is on reads its own access; a child its parent lacks is 404', async () => {
    const { caps } = parentAndChild(true)
    expect(item(await caps.source('package:p/mcp:s'), 'grep')).toMatchObject({ access: 'read', own_access: 'read', reason: null })
    expect(await status(caps.source('package:p/mcp:gone'))).toBe(404)
  })

  it('refresh answers a switched-off source folded', async () => {
    const tool: ItemView = { id: 't', kind: 'tool', name: 't', harness_name: 'mcp__p__t', label: 't', description: null, default: 'outward', default_note: null, suggested: null, access: 'outward', own_access: 'outward', overridden: false, missing: false, allowed: null, fixed: null, reason: null }
    const source: SourceView = { id: 'remote:p', kind: 'remote', title: 'p', enabled: false, switchable: true, options_route: null, listed_at: null, truncated: false, problem: null, checking: false, items: [tool], children: [] }
    const provider = { ...fakeProvider([source]), refresh: async () => ({ test: {} as never, source }) }
    const { source: answered } = await new Capabilities([provider]).refresh('remote:p')
    expect(item(answered, 't')).toMatchObject({ access: 'off', own_access: 'outward' })
  })

  it('refuses to switch builtin:scadbuddy (409)', async () => {
    const { caps, rows } = setup()
    expect(await status(caps.setEnabled(BUILTIN_SOURCES.scadbuddy, false, ctx))).toBe(409)
    expect(rows).toEqual([])
  })

  it('refuses on for a tool, and a tier for nothing (400)', async () => {
    const { caps, rows } = setup()
    expect(await status(caps.setItem(BUILTIN_SOURCES.scadbuddy, 'get_x', 'on', ctx))).toBe(400)
    expect(await status(caps.setItem(BUILTIN_SOURCES.scadbuddy, 'get_x', 'nope' as never, ctx))).toBe(400)
    expect(rows).toEqual([])
  })

  it('refuses an unknown source and an unknown registry tool (404)', async () => {
    const { caps } = setup()
    expect(await status(caps.source('builtin:nope'))).toBe(404)
    expect(await status(caps.setItem('remote:x', 'a', 'read', ctx))).toBe(404)
    expect(await status(caps.setItem(BUILTIN_SOURCES.scadbuddy, 'nope', 'read', ctx))).toBe(404)
    expect(await status(caps.resetItem(BUILTIN_SOURCES.scadbuddy, 'nope', ctx))).toBe(404)
    expect(await status(caps.details(BUILTIN_SOURCES.scadbuddy, 'nope'))).toBe(404)
    await expect(caps.source('builtin:nope')).rejects.toThrow('no source "builtin:nope"')
  })

  it('refresh of a built-in source answers 409', async () => {
    const { caps } = setup()
    await expect(caps.refresh(BUILTIN_SOURCES.scadbuddy)).rejects.toThrow(`"${BUILTIN_SOURCES.scadbuddy}" lists no tools to refresh`)
    expect(await status(caps.refresh(BUILTIN_SOURCES.http))).toBe(409)
    expect(await status(caps.refresh('builtin:nope'))).toBe(404)
  })

  it('details give a registry tool the entry /mcp lists', async () => {
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await createExternalServer(TOOLS, services()).connect(serverSide)
    const client = new Client({ name: 'capabilities-test', version: '0' })
    await client.connect(clientSide)
    const { tools } = await client.listTools()
    await client.close()
    const entry = tools.find((t) => t.name === 'send_x')!

    const listTools = vi.spyOn(Client.prototype, 'listTools')
    const { caps } = setup()
    const details = await caps.details(BUILTIN_SOURCES.scadbuddy, 'send_x')
    expect(details.raw).toEqual(entry)
    expect(details.input_schema).toEqual(entry.inputSchema)
    expect(details.annotations).toEqual(TOOLS[2]!.annotations)
    expect(details.omitted).toEqual([])
    expect(details.item).toMatchObject({ id: 'send_x', access: 'outward' })
    await caps.details(BUILTIN_SOURCES.scadbuddy, 'get_x')
    expect(listTools).toHaveBeenCalledTimes(1)
  })
  it('refuses a write to a fixed item, and an access its allowed list leaves out (409), writing nothing', async () => {
    const base: Omit<ItemView, 'id' | 'name' | 'harness_name' | 'allowed' | 'fixed' | 'reason'> = {
      kind: 'tool',
      label: 'x',
      description: null,
      default: 'outward',
      default_note: null,
      suggested: null,
      access: 'outward',
      own_access: 'outward',
      overridden: false,
      missing: false,
    }
    const pinned: ItemView = { ...base, id: 'dup', name: 'dup', harness_name: 'mcp__p__dup', allowed: null, fixed: { access: 'off', reason: 'two tools share "dup"' }, reason: null }
    const renamed: ItemView = { ...base, id: 'a.b', name: 'a.b', harness_name: 'mcp__p__a_b', allowed: ['off', 'outward'], fixed: null, reason: 'Claude Code renames "a.b"' }
    const source: SourceView = { id: 'remote:p', kind: 'remote', title: 'p', enabled: true, switchable: true, options_route: null, listed_at: null, truncated: false, problem: null, checking: false, items: [pinned, renamed], children: [] }
    const setItem = vi.fn(async () => {})
    const resetItem = vi.fn(async () => {})
    const provider: CapabilityProvider = {
      owns: (id) => id === 'remote:p',
      list: async () => [source],
      get: async (id) => (id === 'remote:p' ? source : undefined),
      setEnabled: async () => {},
      setItem,
      resetItem,
      details: async () => undefined,
    }
    const caps = new Capabilities([provider])
    await expect(caps.setItem('remote:p', 'dup', 'read', ctx)).rejects.toMatchObject({ status: 409, message: 'two tools share "dup"' })
    await expect(caps.resetItem('remote:p', 'dup', ctx)).rejects.toMatchObject({ status: 409, message: 'two tools share "dup"' })
    await expect(caps.setItem('remote:p', 'a.b', 'write', ctx)).rejects.toMatchObject({ status: 409, message: 'Claude Code renames "a.b"' })
    expect(setItem).not.toHaveBeenCalled()
    expect(resetItem).not.toHaveBeenCalled()
    await caps.setItem('remote:p', 'a.b', 'off', ctx)
    expect(setItem).toHaveBeenCalledTimes(1)
  })

  it('a failed listing is not kept: the next call lists again', async () => {
    const tools: readonly Tool[] = [stand('get_y', 'read')]
    const listTools = vi.spyOn(Client.prototype, 'listTools').mockRejectedValueOnce(new Error('listing failed'))
    await expect(listExternal(tools)).rejects.toThrow('listing failed')
    expect([...(await listExternal(tools)).keys()]).toEqual(['get_y'])
    expect(listTools).toHaveBeenCalledTimes(2)
  })
})
