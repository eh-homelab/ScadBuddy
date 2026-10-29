import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { z } from 'zod'
import type { AuditContext } from '../src/audit/log.js'
import { Capabilities, CapabilityError, type SourceView } from '../src/access/capabilities.js'
import { BUILTIN_SOURCES, MIGRATED_BY } from '../src/access/model.js'
import { builtInProviders } from '../src/access/providers.js'
import { BROWSER_ORIGINS_PATH } from '../src/routes/browserOrigins.js'
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

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the capabilities tree', () => {
  it('lists the four built-in sources in order, each tool at its declared tier', async () => {
    const { caps } = setup()
    const tree = await caps.tree()
    expect(tree.map((s) => s.id)).toEqual([BUILTIN_SOURCES.scadbuddy, BUILTIN_SOURCES.browser, BUILTIN_SOURCES.http, BUILTIN_SOURCES.ownPlugin])
    expect(tree.map((s) => s.title)).toEqual(['ScadBuddy tools', 'Your browser tab', 'HTTP requests', 'ScadBuddy skills and subagents'])
    expect(tree.map((s) => s.switchable)).toEqual([false, false, true, true])
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
    expect(tree[3]!.items).toEqual([])
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
    expect(item(await caps.source(BUILTIN_SOURCES.scadbuddy), 'send_x')).toMatchObject({ access: 'outward', overridden: false })
    await caps.setItem(BUILTIN_SOURCES.scadbuddy, 'send_x', 'outward', ctx)
    expect(rows).toEqual([])
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
})
