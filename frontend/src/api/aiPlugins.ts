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
import { ApiError } from './client'

export type RiskTier = 'read' | 'write' | 'outward'

export interface RemotePlugin {
  name: string
  kind: 'remote_mcp'
  url: string
  enabled: boolean
  tool_prefix: string
  auth: { header: string; last4: string } | null
  usable: boolean
  tool_tiers: Record<string, RiskTier>
  disabled_tools: string[]
  created_at: string
  updated_at: string
}

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
  tool_tiers?: Record<string, RiskTier>
  disabled_tools?: string[]
}

export interface PluginToolView {
  name: string
  harness_name: string
  description: string | null
  annotations: Record<string, unknown> | null
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
  hooks: { event: string; type: string; url?: string }[]
  mcp_servers: { name: string; type: string; url: string }[]
  files: string[]
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
  enabled: boolean
  pending: {
    ref: string
    commit_sha: string
    content_hash: string
    review: PackageReview
    diff: FileDiff
  } | null
  created_at: string
  updated_at: string
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

/** The problems a refused install lists (422), or none. */
export function refusalProblems(error: unknown): string[] {
  if (!(error instanceof ApiError)) return []
  const problems = (error.problem as { problems?: unknown }).problems
  return Array.isArray(problems) ? problems.filter((p): p is string => typeof p === 'string') : []
}

const seg = encodeURIComponent
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

export const aiPlugins = {
  listRemote: () => request<RemotePlugin[]>('/plugins'),
  createRemote: (body: RemotePluginCreate) => request<RemotePlugin>('/plugins', json('POST', body)),
  updateRemote: (name: string, body: RemotePluginPatch) =>
    request<RemotePlugin>(`/plugins/${seg(name)}`, json('PATCH', body)),
  deleteRemote: (name: string) => request<void>(`/plugins/${seg(name)}`, { method: 'DELETE' }),
  testRemote: (name: string) => request<PluginTest>(`/plugins/${seg(name)}/test`, { method: 'POST' }),

  listPackages: () => request<PluginPackage[]>('/plugin-packages'),
  installPackage: (source: PackageInstall) =>
    request<PluginPackage>('/plugin-packages', json('POST', { source })),
  approvePackage: (name: string, commit_sha: string, content_hash: string) =>
    request<PluginPackage>(`/plugin-packages/${seg(name)}/approve`, json('POST', { commit_sha, content_hash })),
  setPackageEnabled: (name: string, enabled: boolean) =>
    request<PluginPackage>(`/plugin-packages/${seg(name)}`, json('PATCH', { enabled })),
  repinPackage: (name: string, ref?: string) =>
    request<PluginPackage>(`/plugin-packages/${seg(name)}/repin`, json('POST', ref ? { ref } : {})),
  discardRepin: (name: string) =>
    request<PluginPackage>(`/plugin-packages/${seg(name)}/pending`, { method: 'DELETE' }),
  deletePackage: (name: string) => request<void>(`/plugin-packages/${seg(name)}`, { method: 'DELETE' }),
}
