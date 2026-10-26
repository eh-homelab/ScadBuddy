import type { RiskTier } from '../harness/permissions.js'

// One tool-access model (#2171, spec §4.3): a source (where tools come from) and an item
// (a tool, or `*` for the whole source) have an access, kept in `ai_access`.

export const TOOL_ACCESS = ['off', 'read', 'write', 'outward'] as const
export type ToolAccess = (typeof TOOL_ACCESS)[number]

export function isToolAccess(v: unknown): v is ToolAccess {
  return typeof v === 'string' && (TOOL_ACCESS as readonly string[]).includes(v)
}

/** The item that stands for a whole source: its access is `on` or `off`. */
export const SOURCE_ITEM = '*'
/** `updated_by` of the rows migration 20261026T2201Z_access_copy.sql wrote. */
export const MIGRATED_BY = 'migration:2171'

export const BUILTIN_SOURCES = {
  scadbuddy: 'builtin:scadbuddy',
  browser: 'builtin:browser',
  http: 'builtin:http',
  ownPlugin: 'builtin:own-plugin',
} as const

export function remoteSource(name: string): string {
  return `remote:${name}`
}

export function packageSource(pkg: string): string {
  return `package:${pkg}`
}

export function packageServerSource(pkg: string, server: string): string {
  return `package:${pkg}/mcp:${server}`
}

/** The source of a registry tool: the browser's tools are their own. */
export function registrySource(tool: string): string {
  return tool.startsWith('browser_') ? BUILTIN_SOURCES.browser : BUILTIN_SOURCES.scadbuddy
}

export type AccessRow = { source: string; item: string; access: string; updated_by: string }

/** What Settings stored for one set before ai_access: raised tiers and disabled tools, by bare tool name. */
export type StoredOverrides = { tool_tiers: Record<string, RiskTier>; disabled_tools: string[] }
