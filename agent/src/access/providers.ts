import type { AuditContext } from '../audit/log.js'
import { HTTP_TOOL, HTTP_TOOL_DESCRIPTION, HTTP_TOOL_NAME } from '../harness/httpRequest.js'
import { BROWSER_ORIGINS_PATH } from '../routes/browserOrigins.js'
import { ALL_TOOLS } from '../tools/index.js'
import { listExternal } from '../tools/manifest.js'
import type { Tool } from '../tools/registry.js'
import { harnessToolName, PluginError, type PluginRepo, type PluginSummary, toolPrefix } from '../plugins/registry.js'
import { describeTools, type PluginTest, type PluginToolView } from '../plugins/testConnection.js'
import { type Access, CapabilityError, type CapabilityProvider, type ItemDetails, type ItemView, type SourceView } from './capabilities.js'
import type { ListedTool, ToolListings } from './listings.js'
import { type AccessRow, BUILTIN_SOURCES, isToolAccess, registrySource, remoteSource } from './model.js'
import { isRegistrySource, sourceEnabled, toolAccess } from './resolve.js'
import { AccessError, type AccessReader, type AccessWriter } from './store.js'

// The four built-in sources of the capabilities tree (#2171, Task 3), over ai_access through
// AccessStore. The registry's tools are their sources' items; http_request is builtin:http's
// one item; the own plugin's skills and subagents come with #4757.

type Spec = { id: string; title: string; switchable: boolean; options_route: string | null }

const SPECS: readonly Spec[] = [
  { id: BUILTIN_SOURCES.scadbuddy, title: 'ScadBuddy tools', switchable: false, options_route: null },
  // Its `*` switch is #4757's.
  { id: BUILTIN_SOURCES.browser, title: 'Your browser tab', switchable: false, options_route: BROWSER_ORIGINS_PATH },
  { id: BUILTIN_SOURCES.http, title: 'HTTP requests', switchable: true, options_route: null },
  { id: BUILTIN_SOURCES.ownPlugin, title: 'ScadBuddy skills and subagents', switchable: true, options_route: null },
]

const SOURCE_IDS: readonly string[] = SPECS.map((s) => s.id)

export const HTTP_DEFAULT_NOTE = 'read for GET and HEAD, outward (asks first) for anything else'

const rowOf = (rows: readonly AccessRow[], source: string, item: string): AccessRow | undefined =>
  rows.find((r) => r.source === source && r.item === item)

function registryItem(tool: Tool, rows: readonly AccessRow[]): ItemView {
  // The item's own access: a source `*` switch (builtin:browser's, #4757) is not folded in.
  const access = toolAccess(tool.risk, rowOf(rows, registrySource(tool.name), tool.name), true)
  return {
    id: tool.name,
    kind: 'tool',
    name: tool.name,
    harness_name: `mcp__scadbuddy__${tool.name}`,
    label: tool.label,
    description: tool.description,
    default: tool.risk,
    default_note: null,
    suggested: null,
    access,
    overridden: access !== tool.risk,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
  }
}

function httpItem(rows: readonly AccessRow[]): ItemView {
  const row = rowOf(rows, BUILTIN_SOURCES.http, HTTP_TOOL)
  const access = row !== undefined && isToolAccess(row.access) ? row.access : null
  return {
    id: HTTP_TOOL,
    kind: 'tool',
    name: HTTP_TOOL,
    harness_name: HTTP_TOOL_NAME,
    label: 'Make an HTTP request',
    description: HTTP_TOOL_DESCRIPTION,
    default: null,
    default_note: HTTP_DEFAULT_NOTE,
    suggested: null,
    access,
    overridden: access !== null,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
  }
}

// Every built-in item is a tool until #4757: builtin:own-plugin's skills and subagents take on/off,
// and #4757 replaces this with a write per item kind.
function toolWrite(access: Access): Exclude<Access, 'on'> {
  if (access === 'on') throw new CapabilityError('a tool takes off, read, write or outward, not "on"', 400)
  return access
}

/** AccessError (the store refused the write) as a 400. */
async function stored(write: Promise<void>): Promise<void> {
  try {
    await write
  } catch (err) {
    if (err instanceof AccessError) throw new CapabilityError(err.message, 400)
    throw err
  }
}

export function builtInProviders(deps: { access: AccessReader & AccessWriter; tools?: readonly Tool[] }): CapabilityProvider {
  const tools = deps.tools ?? ALL_TOOLS
  const access = deps.access

  const view = (spec: Spec, rows: readonly AccessRow[]): SourceView => {
    let items: ItemView[] = []
    if (isRegistrySource(spec.id)) items = tools.filter((t) => registrySource(t.name) === spec.id).map((t) => registryItem(t, rows))
    else if (spec.id === BUILTIN_SOURCES.http) items = [httpItem(rows)]
    return {
      id: spec.id,
      kind: 'builtin',
      title: spec.title,
      enabled: sourceEnabled(spec.id, rows),
      switchable: spec.switchable,
      options_route: spec.options_route,
      listed_at: null,
      truncated: false,
      items,
      children: [],
    }
  }

  const get = async (source: string): Promise<SourceView | undefined> => {
    const spec = SPECS.find((s) => s.id === source)
    return spec && view(spec, await access.rows([source]))
  }

  return {
    owns: (source) => SOURCE_IDS.includes(source),
    async list() {
      const rows = await access.rows(SOURCE_IDS)
      return SPECS.map((spec) => view(spec, rows))
    },
    get,
    async setEnabled(source, enabled, context: AuditContext) {
      await stored(access.setEnabled(source, enabled, context))
    },
    async setItem(source, item, value, context) {
      await stored(access.setItem(source, item, toolWrite(value), context))
    },
    async resetItem(source, item, context) {
      await stored(access.resetItem(source, item, context))
    },
    async details(source, item): Promise<ItemDetails | undefined> {
      const found = (await get(source))?.items.find((i) => i.id === item)
      if (found === undefined) return undefined
      const tool = isRegistrySource(source) ? tools.find((t) => t.name === item) : undefined
      if (tool === undefined) {
        return { item: found, input_schema: null, output_schema: null, annotations: null, raw: null, omitted: [] }
      }
      const entry = (await listExternal(deps.tools)).get(tool.name)
      const raw = entry === undefined ? null : structuredClone(entry)
      const schema = (key: string): Record<string, unknown> | null => (raw?.[key] as Record<string, unknown> | undefined) ?? null
      return {
        item: found,
        input_schema: schema('inputSchema'),
        output_schema: schema('outputSchema'),
        annotations: { ...tool.annotations },
        raw,
        omitted: [],
      }
    },
  }
}

// ------------------------------------------------------------------ remote plugins

const REMOTE_PREFIX = remoteSource('')

export const RENAMED_REASON = 'Claude Code renames this tool, so it cannot take a tier: it asks first, or is off'

type Listing = Awaited<ReturnType<ToolListings['get']>>

/** A refusal of the registry's, as the tree answers it; a status the tree has no word for is 400. */
async function registry<T>(write: Promise<T>): Promise<T> {
  try {
    return await write
  } catch (err) {
    if (err instanceof PluginError) throw new CapabilityError(err.message, err.status === 422 ? 400 : err.status)
    throw err
  }
}

/** A stored tool's own access: off when disabled, else its tier; `undefined` without a row. */
function storedAccess(plugin: PluginSummary, tool: string): Exclude<Access, 'on'> | undefined {
  if (plugin.disabled_tools.includes(tool)) return 'off'
  return Object.hasOwn(plugin.tool_tiers, tool) ? plugin.tool_tiers[tool] : undefined
}

/** A stored disabled entry under another name that disables `tool` by its harness name (`a.b` for `a_b`). */
function disabledAs(plugin: PluginSummary, tool: string): string | undefined {
  if (plugin.disabled_tools.includes(tool)) return undefined
  return plugin.disabled_tools.find((d) => harnessToolName(d) === harnessToolName(tool))
}

function remoteItem(plugin: PluginSummary, view: PluginToolView, listed: ListedTool): ItemView {
  const collision = view.tier_source === 'collision'
  const renamed = view.tier_source === 'renamed'
  const other = collision || !view.disabled ? undefined : disabledAs(plugin, view.name)
  // The row takes effect unless the pair is hidden anyway; a renamed tool's tier never does.
  const row = collision ? undefined : view.disabled ? 'off' : view.tier_source === 'explicit' ? view.tier : undefined
  return {
    id: view.name,
    kind: 'tool',
    name: view.name,
    harness_name: view.harness_name,
    label: listed.title ?? view.name,
    description: view.description,
    default: 'outward',
    default_note: null,
    suggested: view.suggested_tier,
    access: collision ? 'off' : (row ?? 'outward'),
    overridden: row !== undefined,
    missing: false,
    allowed: renamed ? ['off', 'outward'] : null,
    fixed: collision ? { access: 'off', reason: `Shares its name with ${view.collides_with.join(', ')}, so both are hidden` } : null,
    reason:
      other !== undefined
        ? `Off because "${other}" is off, which Claude Code names the same: reset "${other}" to change it`
        : renamed
          ? RENAMED_REASON
          : null,
  }
}

/** A tool with stored access the listing does not name (or no listing at all). */
function missingItem(plugin: PluginSummary, tool: string): ItemView {
  const renamed = harnessToolName(tool) !== tool
  const access = storedAccess(plugin, tool) ?? 'outward'
  return {
    id: tool,
    kind: 'tool',
    name: tool,
    harness_name: `${toolPrefix(plugin.name)}${harnessToolName(tool)}`,
    label: tool,
    description: null,
    default: 'outward',
    default_note: null,
    suggested: null,
    access,
    overridden: true,
    missing: true,
    allowed: renamed ? ['off', 'outward'] : null,
    fixed: null,
    reason: renamed ? RENAMED_REASON : null,
  }
}

function remoteView(plugin: PluginSummary, listing: Listing): SourceView {
  const tools = listing?.tools ?? []
  const views = describeTools({ name: plugin.name, toolTiers: plugin.tool_tiers, disabledTools: plugin.disabled_tools }, tools)
  // One row per name: a name listed twice is one item (its id is the write's key).
  const listed = new Set<string>()
  const items: ItemView[] = []
  views.forEach((view, i) => {
    if (listed.has(view.name)) return
    listed.add(view.name)
    items.push(remoteItem(plugin, view, tools[i]!))
  })
  const stored = [...new Set([...Object.keys(plugin.tool_tiers), ...plugin.disabled_tools])].filter((t) => !listed.has(t)).sort()
  return {
    id: remoteSource(plugin.name),
    kind: 'remote',
    title: plugin.name,
    enabled: plugin.enabled,
    switchable: true,
    options_route: null,
    listed_at: listing?.listed_at ?? null,
    truncated: listing?.truncated ?? false,
    items: [...items, ...stored.map((tool) => missingItem(plugin, tool))],
    children: [],
  }
}

const REFRESH_STATUSES: readonly number[] = [400, 404, 409, 429, 503]

/** Every remote MCP plugin as a source `remote:<name>` (#2171, #778), over PluginRepo and the stored listings. */
export function remoteProvider(deps: {
  plugins: PluginRepo
  listings: Pick<ToolListings, 'get' | 'getMany'>
  refresh: (name: string) => Promise<{ status: number; body: PluginTest | { detail: string } }>
}): CapabilityProvider {
  const nameOf = (source: string): string => source.slice(REMOTE_PREFIX.length)

  const get = async (source: string): Promise<SourceView | undefined> => {
    const [plugin, listing] = await Promise.all([deps.plugins.get(nameOf(source)), deps.listings.get(source)])
    return plugin && remoteView(plugin, listing)
  }

  const toolWrite = (access: Access): Exclude<Access, 'on'> => {
    if (access === 'on') throw new CapabilityError('a tool takes off, read, write or outward, not "on"', 400)
    return access
  }

  return {
    owns: (source) => source.startsWith(REMOTE_PREFIX),
    async list() {
      const plugins = await deps.plugins.list()
      const listings = await deps.listings.getMany(plugins.map((p) => remoteSource(p.name)))
      return plugins.map((p) => remoteView(p, listings.get(remoteSource(p.name))))
    },
    get,
    async setEnabled(source, enabled, context) {
      // The secret is not touched, so no key is needed.
      await registry(deps.plugins.update(nameOf(source), { enabled }, undefined, context))
    },
    async setItem(source, item, access, context) {
      await registry(deps.plugins.setToolAccess(nameOf(source), item, toolWrite(access), context))
    },
    async resetItem(source, item, context) {
      await registry(deps.plugins.setToolAccess(nameOf(source), item, null, context))
    },
    async details(source, item): Promise<ItemDetails | undefined> {
      const [plugin, listing] = await Promise.all([deps.plugins.get(nameOf(source)), deps.listings.get(source)])
      const found = plugin && remoteView(plugin, listing).items.find((i) => i.id === item)
      if (found === undefined) return undefined
      const listed = listing?.tools.find((t) => t.name === item)
      return {
        item: found,
        input_schema: listed?.input_schema ?? null,
        output_schema: listed?.output_schema ?? null,
        annotations: listed?.annotations ?? null,
        raw: listed?.raw ?? null,
        omitted: listed?.omitted ?? [],
      }
    },
    async refresh(source) {
      const { status, body } = await deps.refresh(nameOf(source))
      if (status !== 200 || !('ok' in body)) {
        const code = REFRESH_STATUSES.includes(status) ? (status as 400 | 404 | 409 | 429 | 503) : 502
        throw new CapabilityError(body.detail, code)
      }
      const view = await get(source)
      if (view === undefined) throw new CapabilityError(`no source "${source}"`, 404)
      return { test: body, source: view }
    },
  }
}
