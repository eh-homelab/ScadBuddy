import type { AuditContext } from '../audit/log.js'
import type { PluginTest } from '../plugins/testConnection.js'
import type { Access, ItemKind } from './items.js'

// The capabilities tree (#2171, spec §4): every source the agent's tools, hooks, skills and
// subagents come from, each item with its default and its effective access. Providers serve
// the sources; Capabilities dispatches to them and checks every write against the model.

export type { Access, ItemKind } from './items.js'

const TIERS: readonly Access[] = ['off', 'read', 'write', 'outward']
const SWITCH: readonly Access[] = ['off', 'on']

export const ITEM_ACCESS: Record<ItemKind, readonly Access[]> = {
  tool: TIERS,
  builtin: TIERS,
  hook: SWITCH,
  skill: SWITCH,
  subagent: SWITCH,
}

export type ItemView = {
  id: string
  kind: ItemKind
  name: string
  harness_name: string
  label: string
  description: string | null
  /** `null`: the default depends on the call, and `default_note` says how. */
  default: Access | null
  default_note: string | null
  suggested: Access | null
  /**
   * The effective access: `off` while the item's source, or the source it is a child of, is
   * switched off (#4757); `null` only when `default` is `null` and no row takes effect.
   */
  access: Access | null
  /** The item's own access, its source's switch not folded in: what its select shows and edits, and Reset resets. */
  own_access: Access | null
  /** The item's own stored row takes effect (a migrated row the registry rule ignores does not). */
  overridden: boolean
  missing: boolean
  /** Narrows `ITEM_ACCESS[kind]` where something is technically forced (`reason` says why); `null` is the whole kind. */
  allowed: Access[] | null
  /** One forced access: only for tools colliding on one name. */
  fixed: { access: Access; reason: string } | null
  reason: string | null
}

export type SourceKind = 'builtin' | 'remote' | 'package' | 'package_server'

export type SourceView = {
  id: string
  kind: SourceKind
  title: string
  enabled: boolean
  switchable: boolean
  options_route: string | null
  listed_at: string | null
  /** The stored listing is not every tool the source has (a `nextCursor`, or entries dropped); false without one. */
  truncated: boolean
  /**
   * Why a turn does not load the source although it is switched on (#4757: a package refused at
   * load, past the listing budget, named as a built-in); its items say it too. `enabled` is then
   * false, and switching it off still clears its row.
   */
  problem: string | null
  /** Whether a turn would load it is not known yet (#4757): its items read off meanwhile; read it again shortly. */
  checking: boolean
  items: ItemView[]
  children: SourceView[]
}

export type ItemDetails = {
  item: ItemView
  input_schema: Record<string, unknown> | null
  output_schema: Record<string, unknown> | null
  annotations: Record<string, unknown> | null
  raw: Record<string, unknown> | null
  omitted: string[]
}

export class CapabilityError extends Error {
  readonly status: 400 | 404 | 409 | 429 | 502 | 503
  /** Seconds, sent as `Retry-After`. */
  readonly retryAfter: number | undefined
  constructor(message: string, status: 400 | 404 | 409 | 429 | 502 | 503, retryAfter?: number) {
    super(message)
    this.status = status
    this.retryAfter = retryAfter
  }
}

/** A provider answers each item's own access in both `access` and `own_access`; Capabilities folds the switches in. */
export interface CapabilityProvider {
  owns(source: string): boolean
  list(): Promise<SourceView[]>
  get(source: string): Promise<SourceView | undefined>
  setEnabled(source: string, enabled: boolean, context: AuditContext): Promise<void>
  setItem(source: string, item: string, access: Access, context: AuditContext): Promise<void>
  resetItem(source: string, item: string, context: AuditContext): Promise<void>
  details(source: string, item: string): Promise<ItemDetails | undefined>
  refresh?(source: string): Promise<{ test: PluginTest; source: SourceView }>
  /** The source a child source (a package's server) is under, so its switch folds in when the child is asked for by id. */
  parentOf?(source: string): string | undefined
}

const ACCESS_WORDS = (kind: ItemKind): string => ITEM_ACCESS[kind].join(', ')

/**
 * `view` with its source's switch on its items (#4757): while it, or the source above it
 * (`offBy`), is switched off, every item reads `off`, saying so. An item whose own access is
 * already off keeps its own reason (one off through another entry's row); any other reason (a
 * renamed tool's, which `allowed` still shows) gives way. `own_access` keeps what the item's row gives.
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

export class Capabilities {
  private readonly providers: readonly CapabilityProvider[]

  constructor(providers: readonly CapabilityProvider[]) {
    this.providers = providers
  }

  async tree(): Promise<SourceView[]> {
    const lists = await Promise.all(this.providers.map((p) => p.list()))
    return lists.flat().map((view) => switched(view))
  }

  async source(id: string): Promise<SourceView> {
    return (await this.found(id)).source
  }

  async setEnabled(id: string, enabled: boolean, context: AuditContext): Promise<SourceView> {
    const { provider, source } = await this.found(id)
    if (!source.switchable) throw new CapabilityError(`"${id}" cannot be switched off or on`, 409)
    await provider.setEnabled(id, enabled, context)
    return this.source(id)
  }

  async setItem(id: string, item: string, access: Access, context: AuditContext): Promise<SourceView> {
    // The item as its provider answers it: a refusal gives the item's own reason, not its source's switch.
    const { provider, own } = await this.found(id)
    const view = this.item(own, item)
    if (!ITEM_ACCESS[view.kind].includes(access)) {
      throw new CapabilityError(`a ${view.kind} takes ${ACCESS_WORDS(view.kind)}, not ${JSON.stringify(access)}`, 400)
    }
    this.refuseFixed(view)
    if (view.allowed !== null && !view.allowed.includes(access)) {
      throw new CapabilityError(view.reason ?? `"${item}" takes only ${view.allowed.join(', ')}`, 409)
    }
    if (access === view.default) await provider.resetItem(id, item, context)
    else await provider.setItem(id, item, access, context)
    return this.source(id)
  }

  async resetItem(id: string, item: string, context: AuditContext): Promise<SourceView> {
    const { provider, own } = await this.found(id)
    this.refuseFixed(this.item(own, item))
    await provider.resetItem(id, item, context)
    return this.source(id)
  }

  async details(id: string, item: string): Promise<ItemDetails> {
    const provider = this.provider(id)
    const details = await provider.details(id, item)
    if (details === undefined) {
      // Tell an unknown source from an unknown item.
      await this.found(id)
      throw new CapabilityError(`no item "${item}" in "${id}"`, 404)
    }
    // The item as the tree shows it, its source's switch on it.
    const shown = (await this.found(id)).source.items.find((i) => i.id === item)
    return shown === undefined ? details : { ...details, item: shown }
  }

  async refresh(id: string): Promise<{ test: PluginTest; source: SourceView }> {
    const { provider } = await this.found(id)
    if (provider.refresh === undefined) throw new CapabilityError(`"${id}" lists no tools to refresh`, 409)
    const { test } = await provider.refresh(id)
    // The source as the tree shows it after the refresh, its switches folded in.
    return { test, source: (await this.found(id)).source }
  }

  private provider(id: string): CapabilityProvider {
    const provider = this.providers.find((p) => p.owns(id))
    if (provider === undefined) throw new CapabilityError(`no source "${id}"`, 404)
    return provider
  }

  /** `source` as the tree shows it, its switches folded in; `own` as its provider answers it. */
  private async found(id: string): Promise<{ provider: CapabilityProvider; source: SourceView; own: SourceView }> {
    const provider = this.provider(id)
    const parentId = provider.parentOf?.(id)
    if (parentId !== undefined) {
      // A child answered alone has only its own switch: take it from its folded parent.
      const parent = await provider.get(parentId)
      const own = parent?.children.find((c) => c.id === id)
      const source = parent && switched(parent).children.find((c) => c.id === id)
      if (own === undefined || source === undefined) throw new CapabilityError(`no source "${id}"`, 404)
      return { provider, source, own }
    }
    const own = await provider.get(id)
    if (own === undefined) throw new CapabilityError(`no source "${id}"`, 404)
    return { provider, source: switched(own), own }
  }

  private item(source: SourceView, item: string): ItemView {
    const view = source.items.find((i) => i.id === item)
    if (view === undefined) throw new CapabilityError(`no item "${item}" in "${source.id}"`, 404)
    return view
  }

  private refuseFixed(view: ItemView): void {
    if (view.fixed !== null) throw new CapabilityError(view.fixed.reason, 409)
  }
}
