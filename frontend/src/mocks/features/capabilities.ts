import { HttpResponse, http } from 'msw'
import type { PluginTest, PluginToolView, RemotePlugin, RiskTier } from '../../api/aiPlugins'
import {
  ITEM_ACCESS,
  type Access,
  type ItemDetails,
  type ItemView,
  type RefreshResult,
  type SourceView,
} from '../../api/capabilities'
import { remotePluginList, setRemotePluginAccess, toolsFor } from '../aiPlugins'

/**
 * #2171 — the agent's capabilities tree (`/api/v1/ai/capabilities`, agent/src/routes/capabilities.ts,
 * access/capabilities.ts, access/providers.ts). The four built-in sources keep their own rows here;
 * the remote sources are read from `../aiPlugins.ts` on every request and written back through
 * `setRemotePluginAccess`, as the agent's `PluginRepo` dual-writes, so `GET /plugins` agrees. A remote
 * source lists its tools only after a refresh (`toolsFor`). The rules the UI depends on are kept:
 * choosing the default resets; an access the kind does not take is 400; a fixed item, an access
 * outside `allowed` and a switch on a source without one are 409; an unknown source or item is 404.
 */

const base = '/api/v1/ai/capabilities'
const LISTED_AT = '2026-10-29T09:00:00.000Z'
const RENAMED_REASON = 'Claude Code renames this tool, so it cannot take a tier: it asks first, or is off'

type Spec = { id: string; title: string; switchable: boolean; options_route: string | null }

const SPECS: readonly Spec[] = [
  { id: 'builtin:scadbuddy', title: 'ScadBuddy tools', switchable: false, options_route: null },
  { id: 'builtin:browser', title: 'Your browser tab', switchable: false, options_route: '/api/v1/ai/settings/browser-origins' },
  { id: 'builtin:http', title: 'HTTP requests', switchable: true, options_route: null },
  { id: 'builtin:own-plugin', title: 'ScadBuddy skills and subagents', switchable: true, options_route: null },
]

type BuiltInTool = { name: string; label: string; description: string; risk: RiskTier }

const TOOLS: Record<string, readonly BuiltInTool[]> = {
  'builtin:scadbuddy': [
    { name: 'list_models', label: 'List models', description: 'Lists every model in the library.', risk: 'read' },
    { name: 'get_model', label: 'Get a model', description: "Reads one model's details and parameters.", risk: 'read' },
    { name: 'get_readme', label: 'Read a README', description: "Reads a model's README.", risk: 'read' },
    { name: 'list_outputs', label: 'List outputs', description: 'Lists the saved outputs.', risk: 'read' },
    { name: 'save_model', label: 'Save a model', description: "Saves a model's source as a new revision.", risk: 'write' },
    { name: 'save_preset', label: 'Save a preset', description: 'Saves parameter values as a named preset.', risk: 'write' },
    { name: 'render_model', label: 'Render a model', description: 'Renders a model with the given parameters.', risk: 'write' },
    { name: 'send_to_printer', label: 'Send to printer', description: 'Sends an output to a printer and starts the print.', risk: 'outward' },
    { name: 'delete_model', label: 'Delete a model', description: 'Deletes a model and its history.', risk: 'outward' },
    { name: 'get_printer_camera', label: "See a printer's camera", description: "Takes a picture from a printer's camera.", risk: 'read' },
  ],
  'builtin:browser': [
    { name: 'browser_click', label: 'Click in your tab', description: 'Clicks an element on the page you have open.', risk: 'write' },
  ],
}

const HTTP_TOOL = 'http_request'
const HTTP_NOTE = 'read for GET and HEAD, outward (asks first) for anything else'

const state = {
  /** Built-in rows: `<source>\u0000<item>` -> access. */
  rows: new Map<string, Access>(),
  /** Built-in source switches; a source without a row is on. */
  enabled: new Map<string, boolean>(),
  /** Remote plugins refreshed at least once, by name. */
  listed: new Set<string>(),
}

export function reset(): void {
  state.rows.clear()
  state.enabled.clear()
  state.listed.clear()
}
reset()

const key = (source: string, item: string) => `${source}\u0000${item}`
const detail = (status: number, text: string) => HttpResponse.json({ detail: text }, { status })

class Refusal extends Error {
  readonly status: 400 | 404 | 409
  constructor(status: 400 | 404 | 409, message: string) {
    super(message)
    this.status = status
  }
}

// ------------------------------------------------------------------ built-in sources

function builtInItem(source: string, tool: BuiltInTool): ItemView {
  const row = state.rows.get(key(source, tool.name))
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
    access: row ?? tool.risk,
    overridden: row !== undefined,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
  }
}

function httpItem(): ItemView {
  const row = state.rows.get(key('builtin:http', HTTP_TOOL)) ?? null
  return {
    id: HTTP_TOOL,
    kind: 'tool',
    name: HTTP_TOOL,
    harness_name: `mcp__scadbuddy_http__${HTTP_TOOL}`,
    label: 'Make an HTTP request',
    description: 'Make one HTTP request, like curl, to any http or https URL: the internet or the local network.',
    default: null,
    default_note: HTTP_NOTE,
    suggested: null,
    access: row,
    overridden: row !== null,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
  }
}

function builtInView(spec: Spec): SourceView {
  const items = spec.id === 'builtin:http' ? [httpItem()] : (TOOLS[spec.id] ?? []).map((t) => builtInItem(spec.id, t))
  return {
    id: spec.id,
    kind: 'builtin',
    title: spec.title,
    enabled: state.enabled.get(spec.id) ?? true,
    switchable: spec.switchable,
    options_route: spec.options_route,
    listed_at: null,
    truncated: false,
    items,
    children: [],
  }
}

// ------------------------------------------------------------------ remote sources

function listedItem(view: PluginToolView): ItemView {
  const renamed = view.tier_source === 'renamed'
  // The agent's `remoteProvider`: tools colliding on one harness name are fixed off, both.
  const collision = view.tier_source === 'collision'
  const row = collision ? undefined : view.disabled ? 'off' : view.tier_source === 'explicit' ? view.tier : undefined
  return {
    id: view.name,
    kind: 'tool',
    name: view.name,
    harness_name: view.harness_name,
    label: view.title ?? view.name,
    description: view.description,
    default: 'outward',
    default_note: null,
    suggested: view.suggested_tier,
    access: collision ? 'off' : (row ?? 'outward'),
    overridden: row !== undefined,
    missing: false,
    allowed: renamed ? ['off', 'outward'] : null,
    fixed: collision ? { access: 'off', reason: `Shares its name with ${view.collides_with.join(', ')}, so both are hidden` } : null,
    reason: renamed ? RENAMED_REASON : null,
  }
}

function missingItem(plugin: RemotePlugin, tool: string): ItemView {
  const renamed = tool.replace(/[^A-Za-z0-9_-]/g, '_') !== tool
  const access: Access = plugin.disabled_tools.includes(tool) ? 'off' : (plugin.tool_tiers[tool] ?? 'outward')
  return {
    id: tool,
    kind: 'tool',
    name: tool,
    harness_name: `${plugin.tool_prefix}${tool.replace(/[^A-Za-z0-9_-]/g, '_')}`,
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

function remoteView(plugin: RemotePlugin): SourceView {
  const listed = state.listed.has(plugin.name)
  const tools = listed ? toolsFor(plugin) : []
  const names = new Set(tools.map((t) => t.name))
  const stored = [...new Set([...Object.keys(plugin.tool_tiers), ...plugin.disabled_tools])].filter((t) => !names.has(t)).sort()
  return {
    id: `remote:${plugin.name}`,
    kind: 'remote',
    title: plugin.name,
    enabled: plugin.enabled,
    switchable: true,
    options_route: null,
    listed_at: listed ? LISTED_AT : null,
    truncated: false,
    items: [...tools.map(listedItem), ...stored.map((t) => missingItem(plugin, t))],
    children: [],
  }
}

/** The remote plugins as `../aiPlugins.ts` has them now; a removed one forgets its listing. */
function remotePlugins(): RemotePlugin[] {
  const plugins = remotePluginList()
  const names = new Set(plugins.map((p) => p.name))
  for (const name of state.listed) if (!names.has(name)) state.listed.delete(name)
  return plugins
}

// ------------------------------------------------------------------ the tree

function tree(): SourceView[] {
  return [...SPECS.map(builtInView), ...remotePlugins().map(remoteView)]
}

function found(id: string): SourceView {
  const spec = SPECS.find((s) => s.id === id)
  if (spec) return builtInView(spec)
  const plugin = id.startsWith('remote:') ? remotePlugins().find((p) => `remote:${p.name}` === id) : undefined
  if (plugin) return remoteView(plugin)
  throw new Refusal(404, `no source "${id}"`)
}

function itemOf(source: SourceView, item: string): ItemView {
  const view = source.items.find((i) => i.id === item)
  if (!view) throw new Refusal(404, `no item "${item}" in "${source.id}"`)
  return view
}

function pluginOf(source: SourceView): RemotePlugin {
  return remotePluginList().find((p) => `remote:${p.name}` === source.id)!
}

/** The agent's `setToolAccess`: the tool's tier and disabled entry replaced, `null` resets. */
function writeRemote(source: SourceView, item: string, access: Exclude<Access, 'on'> | null): void {
  const plugin = pluginOf(source)
  const tiers = { ...plugin.tool_tiers }
  delete tiers[item]
  const disabled = plugin.disabled_tools.filter((t) => t !== item)
  if (access === 'off') disabled.push(item)
  else if (access !== null) tiers[item] = access
  setRemotePluginAccess(plugin.name, { tool_tiers: tiers, disabled_tools: disabled })
}

function write(source: SourceView, item: string, access: Access | null): void {
  if (access === 'on') throw new Refusal(400, 'a tool takes off, read, write or outward, not "on"')
  if (source.kind === 'remote') writeRemote(source, item, access)
  else if (access === null) state.rows.delete(key(source.id, item))
  else state.rows.set(key(source.id, item), access)
}

function setItem(id: string, item: string, access: Access): SourceView {
  const source = found(id)
  const view = itemOf(source, item)
  if (!ITEM_ACCESS[view.kind].includes(access)) {
    throw new Refusal(400, `a ${view.kind} takes ${ITEM_ACCESS[view.kind].join(', ')}, not ${JSON.stringify(access)}`)
  }
  if (view.fixed) throw new Refusal(409, view.fixed.reason)
  if (view.allowed !== null && !view.allowed.includes(access)) {
    throw new Refusal(409, view.reason ?? `"${item}" takes only ${view.allowed.join(', ')}`)
  }
  write(source, item, access === view.default ? null : access)
  return found(id)
}

function resetItem(id: string, item: string): SourceView {
  const source = found(id)
  const view = itemOf(source, item)
  if (view.fixed) throw new Refusal(409, view.fixed.reason)
  write(source, item, null)
  return found(id)
}

function setEnabled(id: string, enabled: boolean): SourceView {
  const source = found(id)
  if (!source.switchable) throw new Refusal(409, `"${id}" cannot be switched off or on`)
  if (source.kind === 'remote') setRemotePluginAccess(pluginOf(source).name, { enabled })
  else state.enabled.set(id, enabled)
  return found(id)
}

const INPUT_SCHEMA = {
  type: 'object',
  properties: {
    query: { type: 'string', description: 'What to look for.' },
    limit: { type: 'integer', default: 10 },
  },
  required: ['query'],
}

function details(id: string, item: string): ItemDetails {
  const source = found(id)
  const view = itemOf(source, item)
  if (view.missing || view.id === HTTP_TOOL) {
    return { item: view, input_schema: null, output_schema: null, annotations: null, raw: null, omitted: [] }
  }
  if (source.kind === 'remote') {
    const listed = toolsFor(pluginOf(source)).find((t) => t.name === item)!
    const { input_schema, output_schema, annotations, raw, omitted } = listed
    return { item: view, input_schema, output_schema, annotations, raw, omitted }
  }
  const annotations = { readOnlyHint: view.default === 'read' }
  const raw = { name: view.name, description: view.description, inputSchema: INPUT_SCHEMA, annotations }
  return { item: view, input_schema: INPUT_SCHEMA, output_schema: null, annotations, raw, omitted: [] }
}

function refresh(id: string): RefreshResult {
  const source = found(id)
  if (source.kind !== 'remote') throw new Refusal(409, `"${id}" lists no tools to refresh`)
  const plugin = pluginOf(source)
  state.listed.add(plugin.name)
  const test: PluginTest = {
    ok: true,
    detail: `connected; ${toolsFor(plugin).length} tools`,
    duration_ms: 42,
    server: { name: plugin.name, version: '0.4.1' },
    tools: toolsFor(plugin),
    truncated: false,
  }
  return { test, source: found(id) }
}

/** Answers `run`'s value, or its refusal as `{ detail }` with its status. */
function answer(run: () => unknown) {
  try {
    return HttpResponse.json(run() as Record<string, unknown>)
  } catch (err) {
    if (err instanceof Refusal) return detail(err.status, err.message)
    throw err
  }
}

/** The body as the agent's strict zod object would take it, or the 400's detail. */
async function bodyOf<T>(request: Request, field: string, ok: (value: unknown) => value is T): Promise<{ value: T } | { error: string }> {
  let body: unknown
  try {
    body = await request.json()
  } catch {
    return { error: 'body is not valid JSON' }
  }
  if (typeof body !== 'object' || body === null || Object.keys(body).length !== 1 || !ok((body as Record<string, unknown>)[field])) {
    return { error: `${field}: Invalid input` }
  }
  return { value: (body as Record<string, T>)[field]! }
}

const isBoolean = (v: unknown): v is boolean => typeof v === 'boolean'
const isAccess = (v: unknown): v is Access => typeof v === 'string' && ['off', 'read', 'write', 'outward', 'on'].includes(v)
// msw hands path params over decoded.
const param = (value: unknown) => String(value)

export const handlers = [
  http.get(base, () => HttpResponse.json({ sources: tree() })),

  http.get(`${base}/:source`, ({ params }) => answer(() => found(param(params.source)))),

  http.put(`${base}/:source`, async ({ params, request }) => {
    const body = await bodyOf(request, 'enabled', isBoolean)
    if ('error' in body) return detail(400, body.error)
    return answer(() => setEnabled(param(params.source), body.value))
  }),

  http.get(`${base}/:source/items/:item`, ({ params }) => answer(() => details(param(params.source), param(params.item)))),

  http.put(`${base}/:source/items/:item`, async ({ params, request }) => {
    const body = await bodyOf(request, 'access', isAccess)
    if ('error' in body) return detail(400, body.error)
    return answer(() => setItem(param(params.source), param(params.item), body.value))
  }),

  http.delete(`${base}/:source/items/:item`, ({ params }) => answer(() => resetItem(param(params.source), param(params.item)))),

  http.post(`${base}/:source/refresh`, ({ params }) => answer(() => refresh(param(params.source)))),
]
