import type { AuditContext } from '../audit/log.js'
import { builtInNamed } from '../plugins/packages/builtins.js'
import { type PackageListingAnswer, reviewServers, serverCollisions } from '../plugins/packages/listing.js'
import { packageServerName, unofferableNameProblem } from '../plugins/packages/mcp.js'
import type { PackagePin, PackageRepo, PackageView } from '../plugins/packages/store.js'
import type { PackageReview } from '../plugins/packages/vet.js'
import { PluginError } from '../plugins/registry.js'
import { type Access, CapabilityError, type CapabilityProvider, type ItemDetails, type ItemView, type SourceView } from './capabilities.js'
import { builtinItem, hookItem } from './items.js'
import type { ToolListings } from './listings.js'
import { type AccessRow, isToolAccess, packageServerSource, packageSource } from './model.js'
import { listedItems, type Listing, pluginItems } from './providers.js'
import { pluginOverrides, sourceEnabled } from './resolve.js'
import { AccessError, type AccessReader, type AccessWriter } from './store.js'

// Plugin packages as capability sources (#2171, #4757, spec §4): `package:<name>` holds a
// package's hooks, skills (its commands among them), subagents and, for a pin approved with
// `allow_refused`, the Claude Code built-ins it names; each server it declares is a child
// source `package:<name>/mcp:<server>` whose items are its tools. The package's switch is
// `ai_plugin_packages.enabled` (PackageRepo); a server's is its `*` row in ai_access.
//
// Items come from the pin as it loads: a review vetted by this code names them as the runtime
// keys them; an older one (no `described`, a hook without an id) is read again from the
// verified copy (`verifiedReviewOf`). One that cannot be read lists no items, and its package
// reads off, since no turn can load it either.

const PREFIX = packageSource('')
const SERVER = '/mcp:'

/** `package:<p>` or `package:<p>/mcp:<s>` as its parts; a package name holds no `/` (vet.ts SAFE_NAME_RE). */
function parse(source: string): { pkg: string; server: string | undefined } {
  const rest = source.slice(PREFIX.length)
  const at = rest.indexOf(SERVER)
  return at < 0 ? { pkg: rest, server: undefined } : { pkg: rest.slice(0, at), server: rest.slice(at + SERVER.length) }
}

const BUILTIN_NOTE = (tool: string): string => `Claude Code's ${tool}; where several packages name it, the strictest access applies`

/** Whether `review` names a package's items as this code keys them (store.ts `namesItems`). */
function namesItems(review: PackageReview): boolean {
  return review.described !== undefined && review.hooks.every((h) => h.id !== undefined)
}

/** `verifiedReviewOf` with each pin's answer kept by its content hash; a failure is asked again next time. */
export function reviewsByHash(verifiedReviewOf: (pin: PackagePin) => Promise<PackageReview>): (pin: PackagePin) => Promise<PackageReview> {
  const cache = new Map<string, Promise<PackageReview>>()
  return (pin) => {
    const found = cache.get(pin.contentHash)
    if (found) return found
    const read = verifiedReviewOf(pin)
    cache.set(pin.contentHash, read)
    read.catch(() => cache.delete(pin.contentHash))
    return read
  }
}

const item = (fields: Pick<ItemView, 'id' | 'kind' | 'name' | 'harness_name' | 'label' | 'description' | 'default'>, access: Access, overridden: boolean): ItemView => ({
  ...fields,
  default_note: null,
  suggested: null,
  access,
  own_access: access,
  overridden,
  missing: false,
  allowed: null,
  fixed: null,
  reason: null,
})

function hookItems(review: PackageReview, rows: readonly AccessRow[], source: string): ItemView[] {
  const out: ItemView[] = []
  for (const hook of review.hooks) {
    // A hook without an id cannot be named by a row: only an older review has one (namesItems).
    if (hook.id === undefined) continue
    const id = hookItem(hook.event, hook.id)
    if (out.some((i) => i.id === id)) continue
    const row = rows.find((r) => r.source === source && r.item === id)
    const fields = { id, kind: 'hook' as const, name: hook.id, harness_name: id, label: `${hook.event} hook (${hook.type})`, description: hook.url ?? null, default: 'on' as const }
    out.push(item(fields, row?.access === 'off' ? 'off' : 'on', row !== undefined))
  }
  return out
}

function builtinItems(review: PackageReview, rows: readonly AccessRow[], source: string): ItemView[] {
  return (review.builtin_tools ?? []).map((tool) => {
    const id = builtinItem(tool)
    const row = rows.find((r) => r.source === source && r.item === id)
    const access = row !== undefined && isToolAccess(row.access) ? row.access : 'outward'
    const fields = { id, kind: 'builtin' as const, name: tool, harness_name: tool, label: tool, description: BUILTIN_NOTE(tool), default: 'outward' as const }
    return item(fields, access, row !== undefined)
  })
}

/** A server's tools, each `off` and fixed while the server cannot be offered (`reason`). */
function unoffered(items: ItemView[], reason: string): ItemView[] {
  return items.map((i) => ({ ...i, access: 'off', own_access: 'off', fixed: { access: 'off', reason }, reason }))
}

type Shown = { view: PackageView; review: PackageReview | undefined }

export function packageProvider(deps: {
  packages: PackageRepo
  access: AccessReader & AccessWriter
  listings: Pick<ToolListings, 'get' | 'getMany'>
  /** The review vetted from the verified copy of `pin` (main.ts: `installer.acquire` + `vetPackage`), for an older stored one. */
  verifiedReviewOf: (pin: PackagePin) => Promise<PackageReview>
  refresh: (pkg: string, server: string) => Promise<PackageListingAnswer>
  /** The enabled packages a turn would not load now (refused at load, past the listing budget); none when omitted. */
  unloadable?: () => Promise<ReadonlySet<string>>
}): CapabilityProvider {
  const reviewOf = reviewsByHash(deps.verifiedReviewOf)

  /** The review a package's items are read from: the stored one when it names them, else the verified copy's. */
  async function shown(view: PackageView): Promise<Shown> {
    if (namesItems(view.review)) return { view, review: view.review }
    const pin = await deps.packages.pinOf(view.name)
    if (!pin) return { view, review: undefined }
    try {
      return { view, review: await reviewOf(pin) }
    } catch {
      return { view, review: undefined }
    }
  }

  const serversOf = (s: Shown): { server: string; local: boolean }[] => (s.review ? reviewServers(s.review, s.view.allow_refused) : [])

  function build(s: Shown, all: readonly Shown[], rows: readonly AccessRow[], listings: ReadonlyMap<string, NonNullable<Listing>>, unloadable: ReadonlySet<string>): SourceView {
    const { view, review } = s
    const source = packageSource(view.name)
    const items = review
      ? [
          ...hookItems(review, rows, source),
          ...pluginItems(review, rows, source, view.name),
          ...(view.allow_refused ? builtinItems(review, rows, source) : []),
        ]
      : []
    // What a turn loads: names shared by the servers of the packages it would load collide.
    const loads = (p: Shown) => p.view.enabled && p.view.approved && p.review !== undefined && !unloadable.has(p.view.name) && !builtInNamed(p.view.name)
    const colliding = serverCollisions(all.filter(loads).flatMap((p) => serversOf(p).map(({ server }) => ({ pkg: p.view.name, server }))))
    const children = serversOf(s).map(({ server, local }): SourceView => {
      const id = packageServerSource(view.name, server)
      const name = packageServerName(view.name, server)
      const { toolTiers, disabledTools } = pluginOverrides(id, rows)
      const listing = local ? undefined : listings.get(id)
      let tools = listedItems({ name, tool_tiers: toolTiers, disabled_tools: disabledTools }, listing)
      const others = (colliding.get(name) ?? []).filter((o) => !(o.pkg === view.name && o.server === server))
      if (others.length > 0) {
        const named = others.map((o) => `"${o.server}" of plugin package ${o.pkg}`).join(', ')
        tools = unoffered(tools, `Its server is named ${name}, as ${named} ${others.length === 1 ? 'is' : 'are'}, so none of them is offered`)
      } else if (name.includes('__')) {
        tools = unoffered(tools, unofferableNameProblem(view.name, server))
      }
      return {
        id,
        kind: 'package_server',
        title: server,
        enabled: sourceEnabled(id, rows),
        switchable: true,
        options_route: null,
        listed_at: listing?.listed_at ?? null,
        truncated: listing?.truncated ?? false,
        items: tools,
        children: [],
      }
    })
    return {
      id: source,
      kind: 'package',
      title: view.name,
      // On only while a turn would load it: approved, enabled, readable and not refused at load.
      enabled: loads(s),
      switchable: view.approved,
      options_route: null,
      listed_at: null,
      truncated: false,
      items,
      children,
    }
  }

  async function views(only?: string): Promise<SourceView[]> {
    const packages = await deps.packages.list()
    const all = await Promise.all(packages.map(shown))
    const wanted = only === undefined ? all : all.filter((s) => s.view.name === only)
    const sources = wanted.flatMap((s) => [packageSource(s.view.name), ...serversOf(s).map(({ server }) => packageServerSource(s.view.name, server))])
    const [rows, listings, unloadable] = await Promise.all([
      deps.access.rows(sources),
      deps.listings.getMany(sources.filter((id) => id.includes(SERVER))),
      deps.unloadable ? deps.unloadable() : Promise.resolve(new Set<string>()),
    ])
    return wanted.map((s) => build(s, all, rows, listings, unloadable))
  }

  const get = async (source: string): Promise<SourceView | undefined> => {
    const { pkg, server } = parse(source)
    const [view] = await views(pkg)
    if (server === undefined || view === undefined) return view
    return view.children.find((c) => c.id === source)
  }

  /** A write the store or the package store refused, as the tree answers it. */
  async function written(write: Promise<unknown>): Promise<void> {
    try {
      await write
    } catch (err) {
      if (err instanceof AccessError) throw new CapabilityError(err.message, 400)
      if (err instanceof PluginError) throw new CapabilityError(err.message, err.status === 422 ? 400 : err.status)
      throw err
    }
  }

  return {
    owns: (source) => source.startsWith(PREFIX) && parse(source).pkg !== '',
    list: () => views(),
    get,
    parentOf: (source) => {
      const { pkg, server } = parse(source)
      return server === undefined ? undefined : packageSource(pkg)
    },
    async setEnabled(source, enabled, context: AuditContext) {
      const { pkg, server } = parse(source)
      // A package's switch is its row's `enabled` (an unapproved pin's write is the store's 409).
      if (server === undefined) await written(deps.packages.setEnabled(pkg, enabled))
      else await written(deps.access.setEnabled(source, enabled, context))
    },
    async setItem(source, item, access, context) {
      await written(deps.access.setItem(source, item, access, context))
    },
    async resetItem(source, item, context) {
      await written(deps.access.resetItem(source, item, context))
    },
    async details(source, item): Promise<ItemDetails | undefined> {
      const found = (await get(source))?.items.find((i) => i.id === item)
      if (found === undefined) return undefined
      const listed = parse(source).server === undefined ? undefined : (await deps.listings.get(source))?.tools.find((t) => t.name === item)
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
      const { pkg, server } = parse(source)
      if (server === undefined) throw new CapabilityError(`"${source}" lists no tools to refresh`, 409)
      const view = await get(source)
      if (view === undefined) throw new CapabilityError(`no source "${source}"`, 404)
      if (await isLocal(pkg, server)) {
        throw new CapabilityError(`"${source}" is started only by a turn, so its tools are not listed here`, 409)
      }
      const { status, body, retryAfter } = await deps.refresh(pkg, server)
      if (status !== 200 || !('ok' in body)) {
        throw new CapabilityError(body.detail, status === 200 ? 502 : status, retryAfter)
      }
      const after = await get(source)
      if (after === undefined) throw new CapabilityError(`no source "${source}"`, 404)
      return { test: body, source: after }
    },
  }

  async function isLocal(pkg: string, server: string): Promise<boolean> {
    const view = await deps.packages.get(pkg)
    if (!view) return false
    return serversOf(await shown(view)).some((s) => s.server === server && s.local)
  }
}
