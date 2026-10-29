import { describe, expect, it, vi } from 'vitest'
import type { AuditContext } from '../src/audit/log.js'
import { Capabilities, CapabilityError, type SourceView } from '../src/access/capabilities.js'
import { listedTool, toStoredTools, type ListedTool, type StoredTool, type ToolListings } from '../src/access/listings.js'
import { type AccessRow, MIGRATED_BY, remoteSource, SOURCE_ITEM, type ToolAccess } from '../src/access/model.js'
import { remoteProvider } from '../src/access/providers.js'
import { pluginOverrides, sourceEnabled } from '../src/access/resolve.js'
import { PluginError, type PluginPatch, type PluginRepo, type PluginSummary } from '../src/plugins/registry.js'
import type { PluginTest } from '../src/plugins/testConnection.js'

// Remote MCP plugins as capability sources (#2171, #778): the provider over a memory
// PluginRepo and a memory ToolListings. The store's own writes are test/plugins.pg.test.ts.

const ctx: AuditContext = { actor: { kind: 'browser', id: 'browser', label: 'Browser' }, surface: 'http' }
const by = (c?: AuditContext): string => (c ? `${c.actor.kind}:${c.actor.id}` : 'system')

type Listing = { tools: ListedTool[]; listed_at: string; truncated: boolean }

/** A memory PluginRepo whose access lives in ai_access-shaped rows, as PluginStore's does. */
function memoryPlugins(names: readonly string[], initial: AccessRow[] = []) {
  const rows: AccessRow[] = [...initial]
  const summary = (name: string): PluginSummary => {
    const source = remoteSource(name)
    const { toolTiers, disabledTools } = pluginOverrides(source, rows)
    return {
      name,
      kind: 'remote_mcp',
      url: `https://${name}.example/mcp`,
      enabled: sourceEnabled(source, rows),
      auth_header: null,
      secret_last4: null,
      tool_tiers: toolTiers,
      disabled_tools: disabledTools,
      created_at: '2026-10-10T00:00:00.000Z',
      updated_at: '2026-10-10T00:00:00.000Z',
      kekId: null,
    }
  }
  const put = (source: string, item: string, access: string | null, who: string): void => {
    const i = rows.findIndex((r) => r.source === source && r.item === item)
    if (i >= 0) rows.splice(i, 1)
    if (access !== null) rows.push({ source, item, access, updated_by: who })
  }
  const known = (name: string): void => {
    if (!names.includes(name)) throw new PluginError(`no plugin named "${name}"`, 404)
  }
  const reject = () => Promise.reject(new Error('not called'))
  const update = vi.fn(async (name: string, patch: PluginPatch, _kek?: unknown, context?: AuditContext): Promise<PluginSummary> => {
    known(name)
    if (patch.enabled !== undefined) put(remoteSource(name), SOURCE_ITEM, patch.enabled ? 'on' : 'off', by(context))
    return summary(name)
  })
  const setToolAccess = vi.fn(async (name: string, tool: string, access: ToolAccess | null, context?: AuditContext): Promise<PluginSummary> => {
    known(name)
    put(remoteSource(name), tool, access, by(context))
    return summary(name)
  })
  const repo: PluginRepo = {
    list: async () => names.map(summary),
    get: async (name) => (names.includes(name) ? summary(name) : undefined),
    create: reject,
    update,
    setToolAccess,
    delete: reject,
    reveal: reject,
  }
  return { repo, rows, update, setToolAccess }
}

function memoryListings(initial: Record<string, Listing> = {}) {
  const store = new Map(Object.entries(initial))
  const listings: Pick<ToolListings, 'get' | 'getMany' | 'put'> = {
    get: async (source) => store.get(source),
    getMany: async (sources) => new Map([...store].filter(([s]) => sources.includes(s))),
    put: async (source, tools, options = {}) => {
      store.set(source, { tools: tools.map(listedTool), listed_at: '2026-10-29T12:00:00.000Z', truncated: options.truncated ?? false })
    },
  }
  return { listings, store }
}

const listing = (entries: Record<string, unknown>[], listed_at = '2026-10-20T00:00:00.000Z'): Listing => ({
  tools: toStoredTools(entries).map(listedTool),
  listed_at,
  truncated: false,
})

const ENTRIES: Record<string, unknown>[] = [
  { name: 'recall', title: 'Recall memories', description: 'Searches the bank', annotations: { readOnlyHint: true }, inputSchema: { type: 'object' } },
  { name: 'retain', description: 'Stores a memory', inputSchema: { type: 'object' }, 'x-extra': 1 },
]

function okTest(stored: StoredTool[]): PluginTest {
  return { ok: true, detail: 'ok', duration_ms: 1, server: { name: 'hs', version: '1' }, tools: [], truncated: false, stored }
}

function setup(opts: { rows?: AccessRow[]; listings?: Record<string, Listing>; refresh?: (name: string) => Promise<{ status: number; body: PluginTest | { detail: string } }> } = {}) {
  const plugins = memoryPlugins(['hs'], opts.rows)
  const mem = memoryListings(opts.listings ?? { 'remote:hs': listing(ENTRIES) })
  const refresh = vi.fn(opts.refresh ?? (async () => ({ status: 200, body: okTest([]) })))
  const provider = remoteProvider({ plugins: plugins.repo, listings: mem.listings, refresh })
  return { ...plugins, ...mem, refresh, provider, caps: new Capabilities([provider]) }
}

const item = (source: SourceView, id: string) => {
  const found = source.items.find((i) => i.id === id)
  if (!found) throw new Error(`no item ${id}`)
  return found
}

async function refusal(p: Promise<unknown>): Promise<{ status: number; message: string }> {
  try {
    await p
  } catch (err) {
    if (err instanceof CapabilityError) return { status: err.status, message: err.message }
    throw err
  }
  throw new Error('not refused')
}

describe('remote MCP plugins as capability sources', () => {
  it("lists a remote plugin's listed tools with label, description and suggestion", async () => {
    const { caps, provider } = setup()
    expect(provider.owns('remote:hs')).toBe(true)
    expect(provider.owns('builtin:http')).toBe(false)
    const [source] = await caps.tree()
    expect(source).toMatchObject({
      id: 'remote:hs',
      kind: 'remote',
      title: 'hs',
      enabled: false,
      switchable: true,
      options_route: null,
      listed_at: '2026-10-20T00:00:00.000Z',
      children: [],
    })
    expect(source!.items).toEqual([
      {
        id: 'recall',
        kind: 'tool',
        name: 'recall',
        harness_name: 'mcp__hs__recall',
        label: 'Recall memories',
        description: 'Searches the bank',
        default: 'outward',
        default_note: null,
        suggested: 'read',
        access: 'outward',
        overridden: false,
        missing: false,
        allowed: null,
        fixed: null,
        reason: null,
      },
      expect.objectContaining({ id: 'retain', label: 'retain', description: 'Stores a memory', suggested: null, access: 'outward' }),
    ])
  })

  it('a plugin never listed shows only its stored tools', async () => {
    const { caps } = setup({
      listings: {},
      rows: [
        { source: 'remote:hs', item: 'recall', access: 'read', updated_by: 'browser:browser' },
        { source: 'remote:hs', item: 'drop_bank', access: 'off', updated_by: 'browser:browser' },
      ],
    })
    const source = await caps.source('remote:hs')
    expect(source.listed_at).toBeNull()
    expect(source.items.map((i) => [i.id, i.label, i.description, i.access, i.overridden, i.missing])).toEqual([
      ['drop_bank', 'drop_bank', null, 'off', true, true],
      ['recall', 'recall', null, 'read', true, true],
    ])
  })

  it('keeps an item that has a row but is not in the listing, marked missing', async () => {
    const { caps } = setup({ rows: [{ source: 'remote:hs', item: 'forget', access: 'off', updated_by: 'browser:browser' }] })
    const source = await caps.source('remote:hs')
    expect(source.items.map((i) => i.id)).toEqual(['recall', 'retain', 'forget'])
    expect(item(source, 'forget')).toMatchObject({ missing: true, access: 'off', overridden: true, label: 'forget', description: null, harness_name: 'mcp__hs__forget' })
    expect(item(source, 'recall').missing).toBe(false)
    // A missing item can be reset, which drops its row.
    await caps.resetItem('remote:hs', 'forget', ctx)
    expect((await caps.source('remote:hs')).items.map((i) => i.id)).toEqual(['recall', 'retain'])
  })

  it('a tool hidden before the upgrade reads as off', async () => {
    const { caps } = setup({ rows: [{ source: 'remote:hs', item: 'retain', access: 'off', updated_by: MIGRATED_BY }] })
    expect(item(await caps.source('remote:hs'), 'retain')).toMatchObject({ access: 'off', overridden: true, missing: false })
  })

  it('a renamed tool takes off or outward only (read answers 409 with its reason)', async () => {
    const { caps, setToolAccess } = setup({ listings: { 'remote:hs': listing([{ name: 'files.list', annotations: { readOnlyHint: true } }]) } })
    const view = item(await caps.source('remote:hs'), 'files.list')
    const reason = 'Claude Code renames this tool, so it cannot take a tier: it asks first, or is off'
    expect(view).toMatchObject({ harness_name: 'mcp__hs__files_list', allowed: ['off', 'outward'], reason, fixed: null, access: 'outward' })
    expect(await refusal(caps.setItem('remote:hs', 'files.list', 'read', ctx))).toEqual({ status: 409, message: reason })
    expect(setToolAccess).not.toHaveBeenCalled()
    await caps.setItem('remote:hs', 'files.list', 'off', ctx)
    expect(setToolAccess).toHaveBeenLastCalledWith('hs', 'files.list', 'off', ctx)
    expect(item(await caps.source('remote:hs'), 'files.list')).toMatchObject({ access: 'off', overridden: true })
  })

  it('a colliding pair is fixed off and refuses writes (409)', async () => {
    const { caps, setToolAccess } = setup({ listings: { 'remote:hs': listing([{ name: 'files.list' }, { name: 'files_list' }]) } })
    const source = await caps.source('remote:hs')
    expect(item(source, 'files.list')).toMatchObject({ access: 'off', fixed: { access: 'off', reason: 'Shares its name with files_list, so both are hidden' } })
    expect(item(source, 'files_list')).toMatchObject({ access: 'off', fixed: { access: 'off', reason: 'Shares its name with files.list, so both are hidden' } })
    expect((await refusal(caps.setItem('remote:hs', 'files_list', 'read', ctx))).status).toBe(409)
    expect((await refusal(caps.resetItem('remote:hs', 'files_list', ctx))).status).toBe(409)
    expect(setToolAccess).not.toHaveBeenCalled()
  })

  it('a tool listed twice under one name is one row, fixed off, as the forwarder hides it', async () => {
    const { caps, setToolAccess } = setup({ listings: { 'remote:hs': listing([{ name: 'recall' }, { name: 'recall' }, { name: 'retain' }]) } })
    const source = await caps.source('remote:hs')
    expect(source.items.map((i) => i.id)).toEqual(['recall', 'retain'])
    expect(item(source, 'recall')).toMatchObject({ access: 'off', fixed: { access: 'off', reason: 'Shares its name with recall, so both are hidden' } })
    expect((await refusal(caps.setItem('remote:hs', 'recall', 'read', ctx))).status).toBe(409)
    expect(setToolAccess).not.toHaveBeenCalled()
  })

  it('setItem off then read moves the tool between disabled_tools and tool_tiers', async () => {
    const { caps, repo } = setup()
    await caps.setItem('remote:hs', 'recall', 'off', ctx)
    expect(await repo.get('hs')).toMatchObject({ disabled_tools: ['recall'], tool_tiers: {} })
    expect(item(await caps.source('remote:hs'), 'recall')).toMatchObject({ access: 'off', overridden: true })
    await caps.setItem('remote:hs', 'recall', 'read', ctx)
    expect(await repo.get('hs')).toMatchObject({ disabled_tools: [], tool_tiers: { recall: 'read' } })
    expect(item(await caps.source('remote:hs'), 'recall')).toMatchObject({ access: 'read', overridden: true })
    // Its default, outward, is a reset.
    await caps.setItem('remote:hs', 'recall', 'outward', ctx)
    expect(await repo.get('hs')).toMatchObject({ disabled_tools: [], tool_tiers: {} })
    expect(item(await caps.source('remote:hs'), 'recall')).toMatchObject({ access: 'outward', overridden: false })
  })

  it("passes the UI's context to setToolAccess", async () => {
    const { caps, setToolAccess, rows } = setup()
    await caps.setItem('remote:hs', 'retain', 'write', ctx)
    expect(setToolAccess).toHaveBeenLastCalledWith('hs', 'retain', 'write', ctx)
    await caps.resetItem('remote:hs', 'retain', ctx)
    expect(setToolAccess).toHaveBeenLastCalledWith('hs', 'retain', null, ctx)
    await caps.setItem('remote:hs', 'retain', 'read', ctx)
    expect(rows).toEqual([{ source: 'remote:hs', item: 'retain', access: 'read', updated_by: 'browser:browser' }])
  })

  it('switching the source on calls update({ enabled: true })', async () => {
    const { caps, update, rows } = setup()
    const source = await caps.setEnabled('remote:hs', true, ctx)
    expect(update).toHaveBeenCalledWith('hs', { enabled: true }, undefined, ctx)
    expect(source.enabled).toBe(true)
    expect(rows).toEqual([{ source: 'remote:hs', item: '*', access: 'on', updated_by: 'browser:browser' }])
  })

  it('a tool off only through a stored entry of another name says which entry to reset', async () => {
    const { caps } = setup({
      listings: { 'remote:hs': listing([{ name: 'a_b' }]) },
      rows: [{ source: 'remote:hs', item: 'a.b', access: 'off', updated_by: 'browser:browser' }],
    })
    const source = await caps.source('remote:hs')
    expect(item(source, 'a_b')).toMatchObject({
      access: 'off',
      overridden: true,
      reason: 'Off because "a.b" is off, which Claude Code names the same: reset "a.b" to change it',
    })
    expect(item(source, 'a.b')).toMatchObject({ missing: true, access: 'off' })
    await caps.resetItem('remote:hs', 'a.b', ctx)
    expect(item(await caps.source('remote:hs'), 'a_b')).toMatchObject({ access: 'outward', overridden: false, reason: null })
  })

  it('an unknown plugin is 404', async () => {
    const { caps } = setup()
    expect((await refusal(caps.source('remote:gone'))).status).toBe(404)
  })

  it('refresh stores the new listing and answers the source', async () => {
    const stored = toStoredTools([{ name: 'reflect', description: 'Thinks' }])
    // The real refresh is testAndList, which stores a good listing before it answers.
    let put: Pick<ToolListings, 'put'>['put'] = () => Promise.reject(new Error('unset'))
    const { caps, refresh, listings } = setup({
      refresh: async (name) => {
        await put(remoteSource(name), stored)
        return { status: 200, body: okTest(stored) }
      },
    })
    put = listings.put
    const { test, source } = await caps.refresh('remote:hs')
    expect(refresh).toHaveBeenCalledWith('hs')
    expect(test.ok).toBe(true)
    expect(source.listed_at).toBe('2026-10-29T12:00:00.000Z')
    expect(source.items.map((i) => [i.id, i.description, i.missing])).toEqual([['reflect', 'Thinks', false]])
  })

  it('a failed refresh keeps the last listing', async () => {
    const { caps, refresh } = setup({ refresh: async () => ({ status: 400, body: { detail: 'down' } }) })
    expect(await refusal(caps.refresh('remote:hs'))).toEqual({ status: 400, message: 'down' })
    expect(refresh).toHaveBeenCalledWith('hs')
    const source = await caps.source('remote:hs')
    expect(source.listed_at).toBe('2026-10-20T00:00:00.000Z')
    expect(source.items.map((i) => i.id)).toEqual(['recall', 'retain'])
  })

  it('a refresh whose connection test failed answers it and keeps the last listing', async () => {
    const failed: PluginTest = { ...okTest([]), ok: false, detail: 'connection refused', server: null }
    const { caps } = setup({ refresh: async () => ({ status: 200, body: failed }) })
    const { test, source } = await caps.refresh('remote:hs')
    expect(test).toMatchObject({ ok: false, detail: 'connection refused' })
    expect(source.listed_at).toBe('2026-10-20T00:00:00.000Z')
    expect(source.items.map((i) => i.id)).toEqual(['recall', 'retain'])
  })

  it('a refresh answered with another status is 502', async () => {
    const { caps } = setup({ refresh: async () => ({ status: 500, body: { detail: 'boom' } }) })
    expect(await refusal(caps.refresh('remote:hs'))).toEqual({ status: 502, message: 'boom' })
  })

  it('details answer the raw entry as listed', async () => {
    const { caps } = setup()
    const details = await caps.details('remote:hs', 'retain')
    expect(details.raw).toEqual({ name: 'retain', description: 'Stores a memory', inputSchema: { type: 'object' }, 'x-extra': 1 })
    expect(details).toMatchObject({ input_schema: { type: 'object' }, output_schema: null, annotations: null, omitted: [] })
    expect(details.item.id).toBe('retain')
    const recall = await caps.details('remote:hs', 'recall')
    expect(recall.annotations).toEqual({ readOnlyHint: true })
    expect((await refusal(caps.details('remote:hs', 'nope'))).status).toBe(404)
  })
})
