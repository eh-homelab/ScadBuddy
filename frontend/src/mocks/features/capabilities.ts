import { HttpResponse, http } from 'msw'
import {
  isBuiltIn,
  type PackageReview,
  type PluginPackage,
  type PluginTest,
  type PluginToolView,
  type RemotePlugin,
  type RiskTier,
} from '../../api/aiPlugins'
import {
  ITEM_ACCESS,
  type Access,
  type ItemDetails,
  type ItemView,
  type RefreshResult,
  type SourceView,
} from '../../api/capabilities'
import { onPackageSwitch, packageList, remotePluginList, setPackageEnabled, setRemotePluginAccess, toolsFor } from '../aiPlugins'

/**
 * #2171 — the agent's capabilities tree (`/api/v1/ai/capabilities`, agent/src/routes/capabilities.ts,
 * access/capabilities.ts, access/providers.ts). The four built-in sources keep their own rows here;
 * the remote sources are read from `../aiPlugins.ts` on every request and written back through
 * `setRemotePluginAccess`, as the agent's `PluginRepo` dual-writes, so `GET /plugins` agrees. A remote
 * source lists its tools only after a refresh (`toolsFor`). The package sources (`package:<name>`,
 * agent access/packageProvider.ts, #4757) are read from `packageList()` too: their switch is the
 * package's `enabled` (`setPackageEnabled`), and only an approved pin has one; their items are the
 * review's hooks, skills (its commands among them), subagents and, approved with `allow_refused`, its
 * built-ins; each http server is a child `package:<name>/mcp:<server>`, on by default, its tools
 * listed after a refresh. ScadBuddy's own plugin (`builtin:own-plugin`) lists its review's skills and
 * subagents, its switch the built-in package's `enabled`, so the Packages section reads it too.
 * A source switched off reads every item `off`, saying so, its `own_access` kept (agent
 * `switched`). The rules the UI depends on are kept: choosing the default resets; an access the
 * kind does not take is 400; a fixed item, an access outside `allowed` and a switch on a source
 * without one are 409; an unknown source or item is 404. Load checks are not mocked: an enabled,
 * approved package loads unless it is named as a built-in, and no two servers collide.
 */

const base = '/api/v1/ai/capabilities'
const LISTED_AT = '2026-10-29T09:00:00.000Z'
const RENAMED_REASON = 'Claude Code renames this tool, so it cannot take a tier: it asks first, or is off'

type Spec = { id: string; title: string; switchable: boolean; options_route: string | null }

const SPECS: readonly Spec[] = [
  { id: 'builtin:scadbuddy', title: 'ScadBuddy tools', switchable: false, options_route: null },
  { id: 'builtin:browser', title: 'Your browser tab', switchable: true, options_route: '/api/v1/ai/settings/browser-origins' },
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

const OWN_PLUGIN = 'builtin:own-plugin'
const OWN_PLUGIN_NAME = 'scadbuddy'

const HTTP_TOOL = 'http_request'
const HTTP_NOTE = 'read for GET and HEAD, outward (asks first) for anything else'

const state = {
  /** Built-in rows: `<source>\u0000<item>` -> access. */
  rows: new Map<string, Access>(),
  /** Built-in and package server switches; a source without a row is on. */
  enabled: new Map<string, boolean>(),
  /** Remote plugins refreshed at least once, by name. */
  listed: new Set<string>(),
  /** Package servers refreshed at least once, by source id. */
  listedServers: new Set<string>(),
}

/**
 * The state is kept in `sessionStorage` too, so a reload shows what was written, as the agent's
 * rows would (the handlers' own state lives in the page and starts again on a reload). Each
 * Playwright test has its own context, so nothing leaks between tests; `reset` clears it.
 */
const STORAGE_KEY = 'sb-mock-capabilities'

type Stored = {
  rows: [string, Access][]
  enabled: [string, boolean][]
  listed: string[]
  listed_servers?: string[]
  /** The built-in packages' switches (the own plugin's is `builtin:own-plugin`'s). */
  built_ins?: Record<string, boolean>
  remote: Record<string, { tool_tiers: Record<string, RiskTier>; disabled_tools: string[]; enabled: boolean }>
}

function save(): void {
  const stored: Stored = {
    rows: [...state.rows],
    enabled: [...state.enabled],
    listed: [...state.listed],
    listed_servers: [...state.listedServers],
    built_ins: Object.fromEntries(packageList().filter(isBuiltIn).map((p) => [p.name, p.enabled])),
    remote: Object.fromEntries(
      remotePluginList().map((p) => [p.name, { tool_tiers: p.tool_tiers, disabled_tools: p.disabled_tools, enabled: p.enabled }]),
    ),
  }
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(stored))
  } catch {
    // No storage (a private window, a test without it): the state lasts as long as the page.
  }
}

function load(): void {
  let stored: Stored | null
  try {
    stored = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? 'null') as Stored | null
  } catch {
    return
  }
  if (stored === null) return
  for (const [k, access] of stored.rows) state.rows.set(k, access)
  for (const [id, on] of stored.enabled) state.enabled.set(id, on)
  for (const name of stored.listed) state.listed.add(name)
  for (const id of stored.listed_servers ?? []) state.listedServers.add(id)
  for (const [name, enabled] of Object.entries(stored.built_ins ?? {})) setPackageEnabled(name, enabled)
  for (const [name, access] of Object.entries(stored.remote)) setRemotePluginAccess(name, access)
}

/**
 * A built-in row as the #2171 migration copied it (#4994): stored, so it is `overridden` and
 * Reset removes it, though at the tool's own tier it changes nothing. For tests.
 */
export function seedBuiltInRow(source: string, item: string, access: Access): void {
  state.rows.set(key(source, item), access)
  save()
}

export function reset(): void {
  state.rows.clear()
  state.enabled.clear()
  state.listed.clear()
  state.listedServers.clear()
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing stored.
  }
}
load()
// A package switched through `PATCH /plugin-packages` is kept too.
onPackageSwitch(save)

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
    own_access: row ?? tool.risk,
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
    own_access: row,
    overridden: row !== null,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
  }
}

/**
 * A plugin's skill and subagent items (the agent's `pluginItems`): one per skill, command and
 * subagent its review lists, `<plugin>:<name>` as the harness names it; on by default.
 */
function pluginItems(review: PackageReview, source: string, plugin: string): ItemView[] {
  const prefix = `${plugin}:`
  const of = (kind: 'skill' | 'subagent', harnessName: string): ItemView => {
    const name = harnessName.startsWith(prefix) ? harnessName.slice(prefix.length) : harnessName
    const id = `${kind}:${name}`
    const row = state.rows.get(key(source, id))
    const access: Access = row === 'off' ? 'off' : 'on'
    return {
      id,
      kind,
      name,
      harness_name: harnessName,
      label: name,
      description: review.described?.[id] ?? null,
      default: 'on',
      default_note: null,
      suggested: null,
      access,
      own_access: access,
      overridden: row !== undefined,
      missing: false,
      allowed: null,
      fixed: null,
      reason: null,
    }
  }
  const skills = [...new Set([...review.skills, ...review.commands])]
  return [...skills.map((s) => of('skill', s)), ...review.agents.map((a) => of('subagent', a))]
}

const ownPlugin = () => packageList().find((p) => isBuiltIn(p) && p.name === OWN_PLUGIN_NAME)!

function builtInView(spec: Spec): SourceView {
  const items =
    spec.id === 'builtin:http'
      ? [httpItem()]
      : spec.id === OWN_PLUGIN
        ? pluginItems(ownPlugin().review, OWN_PLUGIN, OWN_PLUGIN_NAME)
        : (TOOLS[spec.id] ?? []).map((t) => builtInItem(spec.id, t))
  return {
    id: spec.id,
    kind: 'builtin',
    title: spec.title,
    enabled: !spec.switchable || (spec.id === OWN_PLUGIN ? ownPlugin().enabled : (state.enabled.get(spec.id) ?? true)),
    switchable: spec.switchable,
    options_route: spec.options_route,
    listed_at: null,
    truncated: false,
    problem: null,
    checking: false,
    items,
    children: [],
  }
}

// ------------------------------------------------------------------ remote sources

/** The stored `disabled_tools` entry of another name that Claude Code names as `tool` (the agent's `disabledAs`). */
/** What a listed source's items are read from (the agent's `ToolRows`, with the harness names' prefix). */
type ToolRows = Pick<RemotePlugin, 'name' | 'tool_prefix' | 'tool_tiers' | 'disabled_tools'>

function disabledAs(plugin: ToolRows, tool: string): string | undefined {
  if (plugin.disabled_tools.includes(tool)) return undefined
  const harnessOf = (name: string) => name.replace(/[^A-Za-z0-9_-]/g, '_')
  return plugin.disabled_tools.find((d) => harnessOf(d) === harnessOf(tool))
}

/** The agent's `collisionReason`: the other tools by name (never itself), "both" only for a pair. */
function collisionReason(view: PluginToolView): string {
  const others = [...new Set(view.collides_with)].filter((n) => n !== view.name)
  if (others.length === 0) return 'Listed more than once under this name, so it is hidden'
  const all = view.collides_with.length + 1 > 2 ? 'all of them are' : 'both are'
  return `Shares its name with ${others.join(', ')}, so ${all} hidden`
}

function listedItem(plugin: ToolRows, view: PluginToolView): ItemView {
  const renamed = view.tier_source === 'renamed'
  // The agent's `remoteProvider`: tools colliding on one harness name are fixed off, both.
  const collision = view.tier_source === 'collision'
  const other = collision || !view.disabled ? undefined : disabledAs(plugin, view.name)
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
    own_access: collision ? 'off' : (row ?? 'outward'),
    overridden: row !== undefined,
    missing: false,
    allowed: renamed ? ['off', 'outward'] : null,
    fixed: collision ? { access: 'off', reason: collisionReason(view) } : null,
    reason:
      other !== undefined
        ? `Off because "${other}" is off, which Claude Code names the same: reset "${other}" to change it`
        : renamed
          ? RENAMED_REASON
          : null,
  }
}

function missingItem(plugin: ToolRows, tool: string): ItemView {
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
    own_access: access,
    overridden: true,
    missing: true,
    allowed: renamed ? ['off', 'outward'] : null,
    fixed: null,
    reason: renamed ? RENAMED_REASON : null,
  }
}

/** A listed source's tool items (the agent's `listedItems`): one per listed name, then each stored row the listing lacks. */
function listedItems(plugin: ToolRows, tools: PluginTest['tools']): ItemView[] {
  // One row per name, as the agent's `remoteView`: a name listed twice is one item.
  const names = new Set<string>()
  const items: ItemView[] = []
  for (const tool of tools) {
    if (names.has(tool.name)) continue
    names.add(tool.name)
    items.push(listedItem(plugin, tool))
  }
  const stored = [...new Set([...Object.keys(plugin.tool_tiers), ...plugin.disabled_tools])].filter((t) => !names.has(t)).sort()
  return [...items, ...stored.map((t) => missingItem(plugin, t))]
}

function remoteView(plugin: RemotePlugin): SourceView {
  const listed = state.listed.has(plugin.name)
  return {
    id: `remote:${plugin.name}`,
    kind: 'remote',
    title: plugin.name,
    enabled: plugin.enabled,
    switchable: true,
    options_route: null,
    listed_at: listed ? LISTED_AT : null,
    truncated: false,
    problem: null,
    checking: false,
    items: listedItems(plugin, listed ? toolsFor(plugin) : []),
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

// ------------------------------------------------------------------ package sources

const PACKAGE = 'package:'
const SERVER = '/mcp:'
const harnessOf = (name: string) => name.replace(/[^A-Za-z0-9_-]/g, '_')
const packageSource = (pkg: string) => `${PACKAGE}${pkg}`
const serverSource = (pkg: string, server: string) => `${PACKAGE}${pkg}${SERVER}${server}`
/** The agent's `packageServerName`: what a turn names the server, and its tools' prefix. */
const serverName = (pkg: string, server: string) => `plugin_${harnessOf(pkg)}_${harnessOf(server)}`
const BUILTIN_NOTE = (tool: string) => `Claude Code's ${tool}; where several packages name it, the strictest access applies`
const sourceOfKey = (k: string) => k.slice(0, k.indexOf('\u0000'))

/** `package:<p>` or `package:<p>/mcp:<s>` as its parts. */
function parse(id: string): { pkg: string; server: string | undefined } {
  const rest = id.slice(PACKAGE.length)
  const at = rest.indexOf(SERVER)
  return at < 0 ? { pkg: rest, server: undefined } : { pkg: rest.slice(0, at), server: rest.slice(at + SERVER.length) }
}

/** The installed packages as `../aiPlugins.ts` has them now; a removed one's rows, switches and listings go. */
function installed(): PluginPackage[] {
  const packages = packageList().filter((p): p is PluginPackage => !isBuiltIn(p))
  const names = new Set(packages.map((p) => p.name))
  const gone = (id: string) => id.startsWith(PACKAGE) && !names.has(parse(id).pkg)
  for (const k of [...state.rows.keys()]) if (gone(sourceOfKey(k))) state.rows.delete(k)
  for (const id of [...state.enabled.keys()]) if (gone(id)) state.enabled.delete(id)
  for (const id of [...state.listedServers]) if (gone(id)) state.listedServers.delete(id)
  return packages
}

type LoadState = { kind: 'off' } | { kind: 'problem'; problem: string } | { kind: 'loads' }

/** The agent's `stateOf`, without a load check: off, refused (why), or loads. */
function loadState(pkg: PluginPackage): LoadState {
  if (!pkg.enabled || !pkg.approved) return { kind: 'off' }
  if (packageList().some((p) => isBuiltIn(p) && p.name === pkg.name)) {
    return { kind: 'problem', problem: `plugin package ${pkg.name} was not loaded: "${pkg.name}" is built in; remove the installed package` }
  }
  return { kind: 'loads' }
}

/** The agent's `unoffered`: forced off, saying why. */
const unoffered = (items: ItemView[], reason: string): ItemView[] =>
  items.map((i) => ({ ...i, access: 'off', own_access: 'off', fixed: { access: 'off', reason }, reason }))

type ItemFields = Pick<ItemView, 'id' | 'kind' | 'name' | 'harness_name' | 'label' | 'description' | 'default'>

/** A hook (on/off) or an allowed built-in (a tier, outward by default) with its row. */
function packageItem(fields: ItemFields, source: string): ItemView {
  const row = state.rows.get(key(source, fields.id))
  const access: Access = fields.kind === 'builtin' ? (row ?? 'outward') : row === 'off' ? 'off' : 'on'
  return {
    ...fields,
    default_note: null,
    suggested: null,
    access,
    own_access: access,
    overridden: row !== undefined,
    missing: false,
    allowed: null,
    fixed: null,
    reason: null,
  }
}

function packageItems(pkg: PluginPackage, source: string): ItemView[] {
  const { review } = pkg
  const hooks: ItemView[] = []
  for (const hook of review.hooks) {
    // A hook without an id cannot be named by a row: only an older review has one.
    if (hook.id === undefined) continue
    const id = `hook:${hook.event}:${hook.id}`
    if (hooks.some((i) => i.id === id)) continue
    const label = `${hook.event} hook (${hook.type})`
    hooks.push(packageItem({ id, kind: 'hook', name: hook.id, harness_name: id, label, description: hook.url ?? null, default: 'on' }, source))
  }
  const builtins = (pkg.allow_refused ? (review.builtin_tools ?? []) : []).map((tool) =>
    packageItem(
      { id: `builtin:${tool}`, kind: 'builtin', name: tool, harness_name: tool, label: tool, description: BUILTIN_NOTE(tool), default: 'outward' },
      source,
    ),
  )
  return [...hooks, ...pluginItems(review, source, pkg.name), ...builtins]
}

/** A package server's stored tool rows, as the agent's `pluginOverrides`. */
function serverRows(pkg: string, server: string): ToolRows {
  const id = serverSource(pkg, server)
  const name = serverName(pkg, server)
  const tool_tiers: Record<string, RiskTier> = {}
  const disabled_tools: string[] = []
  for (const [k, access] of state.rows) {
    if (sourceOfKey(k) !== id) continue
    const tool = k.slice(id.length + 1)
    if (access === 'off') disabled_tools.push(tool)
    else if (access !== 'on') tool_tiers[tool] = access
  }
  return { name, tool_prefix: `mcp__${name}__`, tool_tiers, disabled_tools: disabled_tools.sort() }
}

function packageView(pkg: PluginPackage): SourceView {
  const loaded = loadState(pkg)
  const problem = loaded.kind === 'problem' ? loaded.problem : null
  const notLoaded = (items: ItemView[]) => (problem === null ? items : unoffered(items, problem))
  const children = [...new Set(pkg.review.mcp_servers.map((s) => s.name))].map((server): SourceView => {
    const id = serverSource(pkg.name, server)
    const rows = serverRows(pkg.name, server)
    const listed = state.listedServers.has(id)
    return {
      id,
      kind: 'package_server',
      title: server,
      enabled: state.enabled.get(id) ?? true,
      switchable: true,
      options_route: null,
      listed_at: listed ? LISTED_AT : null,
      truncated: false,
      problem,
      checking: false,
      items: notLoaded(listedItems(rows, listed ? toolsFor(rows) : [])),
      children: [],
    }
  })
  return {
    id: packageSource(pkg.name),
    kind: 'package',
    title: pkg.name,
    // Off when switched off, unapproved or refused at load.
    enabled: loaded.kind === 'loads',
    switchable: pkg.approved,
    options_route: null,
    listed_at: null,
    truncated: false,
    problem,
    checking: false,
    items: notLoaded(packageItems(pkg, packageSource(pkg.name))),
    children,
  }
}

// ------------------------------------------------------------------ the tree

/**
 * `view` with its source's switch on its items (the agent's `switched`): while it, or the source
 * above it, is off, every item reads `off`, saying so; an item already off keeps its own reason.
 */
function switched(view: SourceView, offBy?: SourceView): SourceView {
  const off = offBy ?? (view.enabled ? undefined : view)
  const items =
    off === undefined
      ? view.items
      : view.items.map((item) => ({
          ...item,
          access: 'off' as const,
          reason: item.own_access === 'off' && item.reason !== null ? item.reason : `Off while "${off.title}" is switched off`,
        }))
  return { ...view, items, children: view.children.map((child) => switched(child, off)) }
}

function tree(): SourceView[] {
  return [...SPECS.map(builtInView), ...remotePlugins().map(remoteView), ...installed().map(packageView)].map((view) => switched(view))
}

/** A source as its provider answers it, no switch folded in: what a write is checked against. */
function ownFound(id: string): SourceView {
  const spec = SPECS.find((s) => s.id === id)
  if (spec) return builtInView(spec)
  const plugin = id.startsWith('remote:') ? remotePlugins().find((p) => `remote:${p.name}` === id) : undefined
  if (plugin) return remoteView(plugin)
  if (id.startsWith(PACKAGE)) {
    const { pkg, server } = parse(id)
    const match = installed().find((p) => p.name === pkg)
    const view = match && packageView(match)
    const source = server === undefined ? view : view?.children.find((c) => c.id === id)
    if (source) return source
  }
  throw new Refusal(404, `no source "${id}"`)
}

/** A source as the tree shows it: its switch folded in, and a server's package's above it. */
function found(id: string): SourceView {
  const view = ownFound(id)
  if (view.kind !== 'package_server') return switched(view)
  return switched(ownFound(packageSource(parse(id).pkg))).children.find((c) => c.id === id)!
}

function itemOf(source: SourceView, item: string): ItemView {
  const view = source.items.find((i) => i.id === item)
  if (!view) throw new Refusal(404, `no item "${item}" in "${source.id}"`)
  return view
}

function packageOf(source: SourceView): PluginPackage {
  return installed().find((p) => p.name === parse(source.id).pkg)!
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
  if (source.kind === 'remote') writeRemote(source, item, access as Exclude<Access, 'on'> | null)
  else if (access === null) state.rows.delete(key(source.id, item))
  else state.rows.set(key(source.id, item), access)
}

function setItem(id: string, item: string, access: Access): SourceView {
  // The item as its provider answers it: a refusal gives the item's own reason, not its source's switch.
  const source = ownFound(id)
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
  const source = ownFound(id)
  const view = itemOf(source, item)
  if (view.fixed) throw new Refusal(409, view.fixed.reason)
  write(source, item, null)
  return found(id)
}

function setEnabled(id: string, enabled: boolean): SourceView {
  const source = found(id)
  if (!source.switchable) throw new Refusal(409, `"${id}" cannot be switched off or on`)
  if (source.kind === 'remote') setRemotePluginAccess(pluginOf(source).name, { enabled })
  else if (source.kind === 'package') {
    const answer = setPackageEnabled(packageOf(source).name, enabled)
    if (typeof answer === 'string') throw new Refusal(409, answer)
  } else if (id === OWN_PLUGIN) setPackageEnabled(OWN_PLUGIN_NAME, enabled)
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
  if (view.missing || view.id === HTTP_TOOL || source.kind === 'package' || id === OWN_PLUGIN) {
    return { item: view, input_schema: null, output_schema: null, annotations: null, raw: null, omitted: [] }
  }
  if (source.kind === 'remote') {
    const listed = toolsFor(pluginOf(source)).find((t) => t.name === item)!
    const { input_schema, output_schema, annotations, raw, omitted } = listed
    return { item: view, input_schema, output_schema, annotations, raw, omitted }
  }
  if (source.kind === 'package_server') {
    const { pkg, server } = parse(id)
    const listed = toolsFor(serverRows(pkg, server!)).find((t) => t.name === item)!
    const { input_schema, output_schema, annotations, raw, omitted } = listed
    return { item: view, input_schema, output_schema, annotations, raw, omitted }
  }
  const annotations = { readOnlyHint: view.default === 'read' }
  const raw = { name: view.name, description: view.description, inputSchema: INPUT_SCHEMA, annotations }
  return { item: view, input_schema: INPUT_SCHEMA, output_schema: null, annotations, raw, omitted: [] }
}

/** The agent's package listing (plugins/packages/listing.ts, `listingRefusal`): only a server a turn would offer. */
function refreshServer(source: SourceView): RefreshResult {
  const { pkg, server } = parse(source.id)
  const match = packageOf(source)
  if (!match.approved) throw new Refusal(409, 'approve the pin before listing its servers')
  const loaded = loadState(match)
  if (loaded.kind === 'off') throw new Refusal(409, `"${packageSource(pkg)}" is switched off; switch it on to list its tools`)
  if (loaded.kind === 'problem') throw new Refusal(409, loaded.problem)
  if (!(state.enabled.get(source.id) ?? true)) throw new Refusal(409, `"${source.id}" is switched off; switch it on to list its tools`)
  state.listedServers.add(source.id)
  const rows = serverRows(pkg, server!)
  const test: RefreshResult['test'] = {
    ok: true,
    detail: `connected; ${toolsFor(rows).length} tools`,
    duration_ms: 42,
    server: { name: rows.name, version: '0.4.1' },
    truncated: false,
  }
  return { test, source: found(source.id) }
}

function refresh(id: string): RefreshResult {
  const source = found(id)
  if (source.kind === 'package_server') return refreshServer(source)
  if (source.kind !== 'remote') throw new Refusal(409, `"${id}" lists no tools to refresh`)

  const plugin = pluginOf(source)
  state.listed.add(plugin.name)
  // The agent's refresh sends no `tools`: they are the source's items.
  const test: RefreshResult['test'] = {
    ok: true,
    detail: `connected; ${toolsFor(plugin).length} tools`,
    duration_ms: 42,
    server: { name: plugin.name, version: '0.4.1' },
    truncated: false,
  }
  return { test, source: found(id) }
}

/** Answers `run`'s value, or its refusal as `{ detail }` with its status; a write is saved. */
function answer(run: () => unknown, writes = false) {
  try {
    const value = run() as Record<string, unknown>
    if (writes) save()
    return HttpResponse.json(value)
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
    return answer(() => setEnabled(param(params.source), body.value), true)
  }),

  http.get(`${base}/:source/items/:item`, ({ params }) => answer(() => details(param(params.source), param(params.item)))),

  http.put(`${base}/:source/items/:item`, async ({ params, request }) => {
    const body = await bodyOf(request, 'access', isAccess)
    if ('error' in body) return detail(400, body.error)
    return answer(() => setItem(param(params.source), param(params.item), body.value), true)
  }),

  http.delete(`${base}/:source/items/:item`, ({ params }) => answer(() => resetItem(param(params.source), param(params.item)), true)),

  http.post(`${base}/:source/refresh`, ({ params }) => answer(() => refresh(param(params.source)), true)),
]
