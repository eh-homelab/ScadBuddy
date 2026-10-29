import type { AuditContext } from '../audit/log.js'
import type { PluginTest } from '../plugins/testConnection.js'
import type { ItemKind } from './items.js'
import type { ToolAccess } from './model.js'

// The capabilities tree (#2171, spec §4): every source the agent's tools, hooks, skills and
// subagents come from, each item with its default and its effective access. Providers serve
// the sources; Capabilities dispatches to them and checks every write against the model.

export type Access = ToolAccess | 'on'
export type { ItemKind } from './items.js'

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
  /** The effective access; `null` only when `default` is `null` and no row takes effect. */
  access: Access | null
  /** A stored row takes effect (a migrated row the registry rule ignores does not). */
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

export interface CapabilityProvider {
  owns(source: string): boolean
  list(): Promise<SourceView[]>
  get(source: string): Promise<SourceView | undefined>
  setEnabled(source: string, enabled: boolean, context: AuditContext): Promise<void>
  setItem(source: string, item: string, access: Access, context: AuditContext): Promise<void>
  resetItem(source: string, item: string, context: AuditContext): Promise<void>
  details(source: string, item: string): Promise<ItemDetails | undefined>
  refresh?(source: string): Promise<{ test: PluginTest; source: SourceView }>
}

const ACCESS_WORDS = (kind: ItemKind): string => ITEM_ACCESS[kind].join(', ')

export class Capabilities {
  private readonly providers: readonly CapabilityProvider[]

  constructor(providers: readonly CapabilityProvider[]) {
    this.providers = providers
  }

  async tree(): Promise<SourceView[]> {
    const lists = await Promise.all(this.providers.map((p) => p.list()))
    return lists.flat()
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
    const { provider, source } = await this.found(id)
    const view = this.item(source, item)
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
    const { provider, source } = await this.found(id)
    this.refuseFixed(this.item(source, item))
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
    return details
  }

  async refresh(id: string): Promise<{ test: PluginTest; source: SourceView }> {
    const { provider } = await this.found(id)
    if (provider.refresh === undefined) throw new CapabilityError(`"${id}" lists no tools to refresh`, 409)
    return provider.refresh(id)
  }

  private provider(id: string): CapabilityProvider {
    const provider = this.providers.find((p) => p.owns(id))
    if (provider === undefined) throw new CapabilityError(`no source "${id}"`, 404)
    return provider
  }

  private async found(id: string): Promise<{ provider: CapabilityProvider; source: SourceView }> {
    const provider = this.provider(id)
    const source = await provider.get(id)
    if (source === undefined) throw new CapabilityError(`no source "${id}"`, 404)
    return { provider, source }
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
