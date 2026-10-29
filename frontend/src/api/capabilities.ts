/**
 * The agent's capabilities routes (#2171, spec §4): Settings → Tools & plugins' tree of every
 * source the assistant's tools come from, and the user's writes to it. Served under
 * `/api/v1/ai/capabilities` by the agent sidecar, not the backend, so the shapes are written
 * out here from `agent/src/access/capabilities.ts` (`ItemView`, `SourceView`, `ItemDetails`),
 * `agent/src/access/listings.ts` (`Omitted`) and `agent/src/routes/capabilities.ts`.
 *
 * A source or item id is one path segment, percent-encoded. Every write answers the source
 * as it is after it; a refusal is an `ApiError` with the agent's `detail`.
 */
import { aiRequest, type PluginTest } from './aiPlugins'

export type Access = 'off' | 'read' | 'write' | 'outward' | 'on'
export type ItemKind = 'tool' | 'hook' | 'skill' | 'subagent' | 'builtin'

const TIERS: readonly Access[] = ['off', 'read', 'write', 'outward']
const SWITCH: readonly Access[] = ['off', 'on']

/** What each kind of item takes (the agent's table). */
export const ITEM_ACCESS: Record<ItemKind, readonly Access[]> = {
  tool: TIERS,
  builtin: TIERS,
  hook: SWITCH,
  skill: SWITCH,
  subagent: SWITCH,
}

export interface ItemView {
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
  /** A stored row takes effect. */
  overridden: boolean
  /** Stored access for a tool the last listing does not name. */
  missing: boolean
  /** Narrows `ITEM_ACCESS[kind]` where something is technically forced (`reason` says why); `null` is the whole kind. */
  allowed: Access[] | null
  /** One forced access: only for tools colliding on one name. */
  fixed: { access: Access; reason: string } | null
  reason: string | null
}

export type SourceKind = 'builtin' | 'remote' | 'package' | 'package_server'

export interface SourceView {
  id: string
  kind: SourceKind
  title: string
  enabled: boolean
  switchable: boolean
  /** An agent route for the source's own options (the browser tab's origins). */
  options_route: string | null
  listed_at: string | null
  /** The stored listing is not every tool the source has. */
  truncated: boolean
  items: ItemView[]
  children: SourceView[]
}

/** What a cap removed from a listed tool before it was stored (agent `access/listings.ts`). */
export type Omitted = 'description' | 'inputSchema' | 'outputSchema' | 'other' | 'title' | 'annotations' | 'keys'

/** One item with its schemas. Every field but `item` is untrusted server text. */
export interface ItemDetails {
  item: ItemView
  input_schema: Record<string, unknown> | null
  output_schema: Record<string, unknown> | null
  annotations: Record<string, unknown> | null
  raw: Record<string, unknown> | null
  omitted: Omitted[]
}

/** A remote source listed again: the connection test and the source after it. */
export interface RefreshResult {
  test: PluginTest
  source: SourceView
}

const BASE = '/capabilities'
const seg = encodeURIComponent
const sourcePath = (id: string) => `${BASE}/${seg(id)}`
const itemPath = (id: string, item: string) => `${sourcePath(id)}/items/${seg(item)}`
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

export const aiCapabilities = {
  list: async (): Promise<SourceView[]> => (await aiRequest<{ sources: SourceView[] }>(BASE)).sources,
  source: (id: string) => aiRequest<SourceView>(sourcePath(id)),
  setEnabled: (id: string, enabled: boolean) => aiRequest<SourceView>(sourcePath(id), json('PUT', { enabled })),
  setItem: (id: string, item: string, access: Access) => aiRequest<SourceView>(itemPath(id, item), json('PUT', { access })),
  resetItem: (id: string, item: string) => aiRequest<SourceView>(itemPath(id, item), { method: 'DELETE' }),
  details: (id: string, item: string) => aiRequest<ItemDetails>(itemPath(id, item)),
  refresh: (id: string) => aiRequest<RefreshResult>(`${sourcePath(id)}/refresh`, { method: 'POST' }),
}
