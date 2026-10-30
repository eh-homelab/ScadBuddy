/**
 * The agent service's plugin routes (#297), served under `/api/v1/ai/*` by the agent
 * sidecar, not the backend (AI design spec §4.2). They are not in the backend's
 * OpenAPI document, so their shapes are written out here, from
 * `agent/src/routes/plugins.ts` (remote MCP endpoints, `PluginView`),
 * `agent/src/plugins/testConnection.ts` (`PluginTest`) and
 * `agent/src/plugins/packages/store.ts` (plugin packages, `PackageView`).
 *
 * Errors come back as `{ detail }`, and a refused package install as
 * `{ detail, problems: string[] }` (422); `readProblem` keeps both on `ApiError.problem`.
 */
import type { Omitted } from './capabilities'
import { ApiError, command } from './client'

export type RiskTier = 'read' | 'write' | 'outward'

export interface RemotePlugin {
  name: string
  kind: 'remote_mcp'
  built_in?: false
  url: string
  enabled: boolean
  tool_prefix: string
  auth: { header: string; last4: string } | null
  usable: boolean
  /** Why it is never handed to the harness (a name Claude Code now reserves, #2127), or null. */
  problem: string | null
  tool_tiers: Record<string, RiskTier>
  disabled_tools: string[]
  created_at: string
  updated_at: string
}

/** One tool of a built-in set, with the tier ScadBuddy's code gives it (its default). */
export interface BuiltInTool {
  name: string
  harness_name: string
  risk: RiskTier
}

/**
 * ScadBuddy's own tool sets (#1953, agent/src/plugins/builtInTools.ts `BuiltInPluginView`), listed
 * first in `/plugins`. Kept only so a `/plugins` read can tell them from remote plugins: their
 * tools' access is edited through the capabilities routes (`./capabilities.ts`, #2171).
 */
export interface BuiltInToolSet {
  name: string
  kind: 'built_in'
  built_in: true
  enabled: boolean
  switchable: boolean
  tool_prefix: string
  tools: BuiltInTool[]
  tool_tiers: Record<string, RiskTier>
  disabled_tools: string[]
}

export type ListedPlugin = BuiltInToolSet | RemotePlugin

export interface RemotePluginCreate {
  name: string
  url: string
  auth_header?: string
  secret?: string
}

export interface RemotePluginPatch {
  url?: string
  enabled?: boolean
  auth_header?: string | null
  secret?: string | null
}

export interface PluginToolView {
  name: string
  harness_name: string
  title: string | null
  description: string | null
  annotations: Record<string, unknown> | null
  input_schema: Record<string, unknown> | null
  output_schema: Record<string, unknown> | null
  /** The server's entry as listed, minus what a cap removed (agent `access/listings.ts`). */
  raw: Record<string, unknown>
  /** What a cap removed from `raw`. */
  omitted: Omitted[]
  tier: RiskTier
  tier_source: 'explicit' | 'default' | 'renamed' | 'collision'
  collides_with: string[]
  suggested_tier: RiskTier | null
  disabled: boolean
}

export interface PluginTest {
  ok: boolean
  detail: string
  duration_ms: number
  server: { name: string; version: string } | null
  tools: PluginToolView[]
  truncated: boolean
}

export interface PackageReview {
  name: string
  description: string | null
  version: string | null
  skills: string[]
  commands: string[]
  agents: string[]
  /** `id` names its access row, `hook:<event>:<id>`; absent in a review stored before #4757. */
  hooks: { event: string; type: string; url?: string; id?: string; matcher?: string }[]
  mcp_servers: { name: string; type: string; url: string }[]
  /** Every other server it declares, by name: a local (stdio) one, offered only with `allow_refused` (#4757); absent in an older review. */
  local_servers?: string[]
  files: string[]
  /**
   * What the vetting refuses (a command hook, a hooks module, a local MCP server, ...).
   * Such a pin loads only when approved with `allow_refused`, as it is.
   */
  refused?: string[]
  /** The Claude Code built-ins its skills and subagents name, offered while the pin is allowed. */
  builtin_tools?: string[]
  /** Each item's id (`skill:<name>` for a skill or command, `subagent:<name>`) to its frontmatter description (#4757). */
  described?: Record<string, string | null>
  // `aliases` (vet.ts `PackageReview.aliases`) is left out on purpose: the browser never reads it.
}

export interface FileDiff {
  added: string[]
  removed: string[]
  changed: string[]
}

export type PackageSource =
  | { kind: 'git'; url: string; ref: string; path: string }
  | { kind: 'marketplace'; url: string; ref: string; entry: string; plugin_url: string; plugin_path: string }

export interface PluginPackage {
  name: string
  source: PackageSource
  commit_sha: string
  content_hash: string
  review: PackageReview
  approved: boolean
  approved_at: string | null
  /** Approved with what `review.refused` lists allowed: its code runs with the Claude credential. */
  allow_refused: boolean
  enabled: boolean
  pending: {
    ref: string
    /** Where the re-pin is fetched from; replaces the pin's on approval. */
    plugin_url: string
    plugin_path: string
    commit_sha: string
    content_hash: string
    review: PackageReview
    diff: FileDiff
  } | null
  created_at: string
  updated_at: string
}

/** Where a package's pinned files are fetched from: the repository and the path in it. */
export function packageFetch(pkg: PluginPackage): { url: string; path: string } {
  return pkg.source.kind === 'marketplace'
    ? { url: pkg.source.plugin_url, path: pkg.source.plugin_path }
    : { url: pkg.source.url, path: pkg.source.path }
}

/** A pending re-pin fetched from another repository or path than the pin. */
export function repinMoves(pkg: PluginPackage): boolean {
  if (!pkg.pending) return false
  const { url, path } = packageFetch(pkg)
  return pkg.pending.plugin_url !== url || pkg.pending.plugin_path !== path
}

/**
 * A plugin that ships with the agent (agent `plugins/packages/builtins.ts`): ScadBuddy's
 * own. Listed first; it can be enabled or disabled, never
 * removed, approved or re-pinned.
 */
export interface BuiltInPluginPackage {
  name: string
  built_in: true
  source: { kind: 'built_in'; path: string }
  review: PackageReview
  approved: true
  enabled: boolean
}

export type ListedPackage = PluginPackage | BuiltInPluginPackage

export function isBuiltIn(pkg: ListedPackage): pkg is BuiltInPluginPackage {
  return 'built_in' in pkg && pkg.built_in === true
}

/** An install that named a built-in plugin: answered 409 with `built_in: true`, not refused. */
export function isBuiltInAnswer(error: unknown): boolean {
  return error instanceof ApiError && (error.problem as { built_in?: unknown }).built_in === true
}

/** A file of a package, for review (#1029; agent `plugins/packages/files.ts`). */
export interface PackageFile {
  path: string
  size: number
}

/** One file's content: `null` when it is binary, cut at a preview unless asked for all. */
export interface PackageFileContent {
  path: string
  size: number
  binary: boolean
  media_type: string
  truncated: boolean
  /** Too large to send whole (#2151): only the preview, so no "Show all". */
  too_large: boolean
  content: string | null
}

/** Which pin's files: the package's own, or its pending re-pin's. */
export interface PackageFilesOf {
  name: string
  pending: boolean
}

export type PackageInstall =
  | { kind: 'git'; url: string; ref?: string; path?: string }
  | { kind: 'marketplace'; url: string; ref?: string; entry: string }

export const AI_BASE = '/api/v1/ai'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${AI_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    let body: { detail?: string; problems?: unknown } = {}
    try {
      body = (await response.json()) as typeof body
    } catch {
      // not JSON: the status text below
    }
    throw new ApiError({
      ...body,
      title: response.statusText || 'Request failed',
      status: response.status,
      detail: body.detail ?? response.statusText,
    })
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** The same request for the agent's other route groups (`api/capabilities.ts`). */
export { request as aiRequest }

/** The problems a refused install lists (422), or none. */
export function refusalProblems(error: unknown): string[] {
  if (!(error instanceof ApiError)) return []
  const problems = (error.problem as { problems?: unknown }).problems
  return Array.isArray(problems) ? problems.filter((p): p is string => typeof p === 'string') : []
}

const seg = encodeURIComponent
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

export const aiPlugins = {
  listPlugins: () => request<ListedPlugin[]>('/plugins'),
  createRemote: (body: RemotePluginCreate) => request<RemotePlugin>('/plugins', json('POST', body)),
  deleteRemote: (name: string) => request<void>(`/plugins/${seg(name)}`, { method: 'DELETE' }),

  listPackages: () => request<ListedPackage[]>('/plugin-packages'),
  // The two fetches are the agent's commands (#1055): an Idempotency-Key per press, a
  // 202 followed at the agent's /api/v1/ai/operations/{id}.
  installPackage: (source: PackageInstall) =>
    command<PluginPackage>('/ai/plugin-packages', json('POST', { source }), '/ai/operations'),
  approvePackage: (name: string, commit_sha: string, content_hash: string, allow_refused = false) =>
    request<PluginPackage>(
      `/plugin-packages/${seg(name)}/approve`,
      json('POST', allow_refused ? { commit_sha, content_hash, allow_refused } : { commit_sha, content_hash }),
    ),
  setPackageEnabled: <T extends ListedPackage = PluginPackage>(name: string, enabled: boolean) =>
    request<T>(`/plugin-packages/${seg(name)}`, json('PATCH', { enabled })),
  repinPackage: (name: string, ref?: string) =>
    command<PluginPackage>(`/ai/plugin-packages/${seg(name)}/repin`, json('POST', ref ? { ref } : {}), '/ai/operations'),
  discardRepin: (name: string) =>
    request<PluginPackage>(`/plugin-packages/${seg(name)}/pending`, { method: 'DELETE' }),
  deletePackage: (name: string) => request<void>(`/plugin-packages/${seg(name)}`, { method: 'DELETE' }),
  packageFiles: ({ name, pending }: PackageFilesOf) =>
    request<{ files: PackageFile[] }>(`/plugin-packages/${seg(name)}/files${pending ? '?pending=true' : ''}`),
  packageFile: ({ name, pending }: PackageFilesOf, path: string, full = false) => {
    const query = new URLSearchParams({ path })
    if (pending) query.set('pending', 'true')
    if (full) query.set('full', 'true')
    return request<PackageFileContent>(`/plugin-packages/${seg(name)}/file?${query}`)
  },
}
