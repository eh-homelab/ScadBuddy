import type { AuditContext } from '../audit/log.js'
import { builtInNamed } from '../plugins/packages/builtins.js'
import type { LoadCheck } from '../plugins/packages/install.js'
import type { PackageListingAnswer } from '../plugins/packages/listing.js'
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
// cached copy (`verifiedReviewOf`, read only). One that cannot be read yet lists no items; an
// enabled package reads as not yet checked meanwhile (its load check says why it does not load).
//
// Whether a turn would load a package (`stateOf`) is one rule for the tree and for listing a
// server (`listingRefusal`): an unchecked package is never shown as loadable, and a server a turn
// would not offer is never connected to.

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
  // `local_servers` came after `described`: a review without it cannot say which servers a turn starts.
  return review.described !== undefined && review.local_servers !== undefined && review.hooks.every((h) => h.id !== undefined)
}

const NOTHING_CHECKED: LoadCheck = { problems: new Map(), checking: new Set() }

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

/** The servers a package's review says a turn offers: its http ones, and its local ones while it is allowed. */
export function reviewServers(review: PackageReview, allowed: boolean): { server: string; local: boolean }[] {
  const out: { server: string; local: boolean }[] = []
  for (const { name } of review.mcp_servers) if (!out.some((s) => s.server === name)) out.push({ server: name, local: false })
  if (allowed) for (const name of review.local_servers ?? []) if (!out.some((s) => s.server === name)) out.push({ server: name, local: true })
  return out
}

/**
 * The server names (`packageServerName`) more than one of `servers` would carry, with their
 * owners: mcp.ts `collidingServerNames` over what the packages a turn loads offer.
 */
export function serverCollisions(servers: readonly { pkg: string; server: string }[]): Map<string, { pkg: string; server: string }[]> {
  const owners = new Map<string, { pkg: string; server: string }[]>()
  for (const { pkg, server } of servers) {
    const name = packageServerName(pkg, server)
    const list = owners.get(name) ?? []
    if (!list.some((o) => o.pkg === pkg && o.server === server)) list.push({ pkg, server })
    owners.set(name, list)
  }
  return new Map([...owners].filter(([, list]) => list.length > 1))
}

type Shown = { view: PackageView; review: PackageReview | undefined }

/**
 * Whether a turn would load a package now: `off` (its row is switched off or its pin unapproved),
 * `problem` (refused at load, past the listing budget, named as a built-in: why), `checking` (not
 * yet known, never shown as loadable), or `loads`.
 */
type LoadState = { kind: 'off' } | { kind: 'problem'; problem: string } | { kind: 'checking' } | { kind: 'loads' }

export const checkingReason = (pkg: string): string => `Checking whether "${pkg}" can load…`

const switchedOff = (id: string): string => `"${id}" is switched off; switch it on to list its tools`

/** A package's items and its servers' tools while it is not known to load: off, saying why. */
function notLoaded(items: ItemView[], state: LoadState, pkg: string): ItemView[] {
  // Refused at load: forced off. Not yet checked: off for now, each keeping its own access.
  if (state.kind === 'problem') return unoffered(items, state.problem)
  if (state.kind === 'checking') return items.map((i) => ({ ...i, access: 'off', reason: checkingReason(pkg) }))
  return items
}

type Built = { source: SourceView; state: LoadState; blocked: ReadonlyMap<string, string> }

export function packageProvider(deps: {
  packages: PackageRepo
  access: AccessReader & AccessWriter
  listings: Pick<ToolListings, 'get' | 'getMany'>
  /** The review vetted from the verified copy of `pin` (main.ts: `installer.acquire` + `vetPackage`), for an older stored one. */
  verifiedReviewOf: (pin: PackagePin) => Promise<PackageReview>
  refresh: (pkg: string, server: string) => Promise<PackageListingAnswer>
  /** What a turn would make of the enabled packages now (install.ts `packageLoadCheck`); every one loads when omitted. */
  loadCheck?: () => Promise<LoadCheck>
}): CapabilityProvider & { listingRefusal(pkg: string, server: string): Promise<string | undefined> } {
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

  function stateOf(s: Shown, check: LoadCheck): LoadState {
    const { name, enabled, approved } = s.view
    if (!enabled || !approved) return { kind: 'off' }
    if (builtInNamed(name)) return { kind: 'problem', problem: `plugin package ${name} was not loaded: "${name}" is built in; remove the installed package` }
    const problem = check.problems.get(name)
    if (problem !== undefined) return { kind: 'problem', problem }
    // A review not yet readable is a copy not yet cached: as unknown as an unchecked pin.
    if (check.checking.has(name) || s.review === undefined) return { kind: 'checking' }
    return { kind: 'loads' }
  }

  function build(s: Shown, all: readonly Shown[], rows: readonly AccessRow[], listings: ReadonlyMap<string, NonNullable<Listing>>, check: LoadCheck): Built {
    const { view, review } = s
    const source = packageSource(view.name)
    const state = stateOf(s, check)
    const items = review
      ? [
          ...hookItems(review, rows, source),
          ...pluginItems(review, rows, source, view.name),
          ...(view.allow_refused ? builtinItems(review, rows, source) : []),
        ]
      : []
    // What a turn may load: names shared by the servers of those packages collide (an unchecked one counts).
    const mayLoad = (p: Shown) => ['loads', 'checking'].includes(stateOf(p, check).kind)
    const colliding = serverCollisions(all.filter(mayLoad).flatMap((p) => serversOf(p).map(({ server }) => ({ pkg: p.view.name, server }))))
    const blocked = new Map<string, string>()
    const children = serversOf(s).map(({ server, local }): SourceView => {
      const id = packageServerSource(view.name, server)
      const name = packageServerName(view.name, server)
      const { toolTiers, disabledTools } = pluginOverrides(id, rows)
      const listing = local ? undefined : listings.get(id)
      let tools = listedItems({ name, tool_tiers: toolTiers, disabled_tools: disabledTools }, listing)
      const others = (colliding.get(name) ?? []).filter((o) => !(o.pkg === view.name && o.server === server))
      if (others.length > 0) {
        const named = others.map((o) => `"${o.server}" of plugin package ${o.pkg}`).join(', ')
        blocked.set(id, `Its server is named ${name}, as ${named} ${others.length === 1 ? 'is' : 'are'}, so none of them is offered`)
      } else if (name.includes('__')) {
        blocked.set(id, unofferableNameProblem(view.name, server))
      }
      const reason = blocked.get(id)
      tools = notLoaded(reason === undefined ? tools : unoffered(tools, reason), state, view.name)
      return {
        id,
        kind: 'package_server',
        title: server,
        enabled: sourceEnabled(id, rows),
        switchable: true,
        options_route: null,
        listed_at: listing?.listed_at ?? null,
        truncated: listing?.truncated ?? false,
        problem: state.kind === 'problem' ? state.problem : null,
        checking: state.kind === 'checking',
        items: tools,
        children: [],
      }
    })
    const built: SourceView = {
      id: source,
      kind: 'package',
      title: view.name,
      // Off when switched off, unapproved or refused at load; while unchecked, on with its items off.
      enabled: state.kind === 'loads' || state.kind === 'checking',
      switchable: view.approved,
      options_route: null,
      listed_at: null,
      truncated: false,
      problem: state.kind === 'problem' ? state.problem : null,
      checking: state.kind === 'checking',
      items: notLoaded(items, state, view.name),
      children,
    }
    return { source: built, state, blocked }
  }

  async function views(only?: string): Promise<Built[]> {
    const packages = await deps.packages.list()
    const all = await Promise.all(packages.map(shown))
    const wanted = only === undefined ? all : all.filter((s) => s.view.name === only)
    const sources = wanted.flatMap((s) => [packageSource(s.view.name), ...serversOf(s).map(({ server }) => packageServerSource(s.view.name, server))])
    const [rows, listings, check] = await Promise.all([
      deps.access.rows(sources),
      deps.listings.getMany(sources.filter((id) => id.includes(SERVER))),
      // A check that fails says as little as one not yet done: every enabled package is unchecked.
      deps.loadCheck ? deps.loadCheck().catch((): LoadCheck => ({ problems: new Map(), checking: new Set(packages.map((p) => p.name)) })) : Promise.resolve(NOTHING_CHECKED),
    ])
    return wanted.map((s) => build(s, all, rows, listings, check))
  }

  const get = async (source: string): Promise<SourceView | undefined> => {
    const { pkg, server } = parse(source)
    const [built] = await views(pkg)
    if (server === undefined || built === undefined) return built?.source
    return built.source.children.find((c) => c.id === source)
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
    list: async () => (await views()).map((b) => b.source),
    async listingRefusal(pkg, server) {
      const [built] = await views(pkg)
      if (built === undefined) return undefined
      const { source, state, blocked } = built
      if (state.kind === 'off') return switchedOff(source.id)
      if (state.kind === 'problem') return state.problem
      if (state.kind === 'checking') return `${checkingReason(pkg)} Try again shortly`
      const child = source.children.find((c) => c.id === packageServerSource(pkg, server))
      if (child === undefined) return undefined
      if (!child.enabled) return switchedOff(child.id)
      return blocked.get(child.id)
    },
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
