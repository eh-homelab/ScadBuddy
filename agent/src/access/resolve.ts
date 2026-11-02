import { RISK_TIERS, type RiskTier } from '../harness/permissions.js'
import { type AccessRow, BUILTIN_SOURCES, isToolAccess, MIGRATED_BY, registrySource, SOURCE_ITEM, type StoredOverrides, type ToolAccess } from './model.js'

// The resolution rules of the access model (#2171, spec §4.1–§4.3): pure, no I/O.
// agent-durable applies the same rules; test/fixtures/access-vectors.json pins the two together.

const REGISTRY_SOURCES: readonly string[] = [BUILTIN_SOURCES.scadbuddy, BUILTIN_SOURCES.browser]

/**
 * A tool's effective access: the row's when it holds one, else the declared tier. With
 * `migratedRaisesOnly` (a registry source), a row the migration copied counts only when it is
 * `off` or above `declared`: the old registry overrides could only raise. Kept for good: a
 * copied tier the old code ignored must not start to lower a tool's gate (#4828).
 */
export function toolAccess(declared: RiskTier, row: AccessRow | undefined, migratedRaisesOnly: boolean): ToolAccess {
  if (row === undefined || !isToolAccess(row.access)) return declared
  if (migratedRaisesOnly && row.updated_by === MIGRATED_BY && row.access !== 'off') {
    return RISK_TIERS.indexOf(row.access) > RISK_TIERS.indexOf(declared) ? row.access : declared
  }
  return row.access
}

/**
 * The registry tools' overrides as the gates apply them: `itemOverrides`, with every
 * browser tool `off` while `builtin:browser` is switched off (#4757).
 */
export function registryOverrides(tools: readonly { name: string; risk: RiskTier }[], rows: readonly AccessRow[]): StoredOverrides {
  const overrides = itemOverrides(tools, rows)
  if (sourceEnabled(BUILTIN_SOURCES.browser, rows)) return overrides
  const browser = (name: string): boolean => registrySource(name) === BUILTIN_SOURCES.browser
  const tool_tiers = Object.create(null) as Record<string, RiskTier>
  for (const [name, tier] of Object.entries(overrides.tool_tiers)) if (!browser(name)) tool_tiers[name] = tier
  const disabled = new Set([...overrides.disabled_tools, ...tools.map((t) => t.name).filter(browser)])
  return { tool_tiers, disabled_tools: [...disabled].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)) }
}

/**
 * Each registry tool's own access, no source switch folded in: every effective tier that
 * differs from the tool's own, and every `off`.
 */
export function itemOverrides(tools: readonly { name: string; risk: RiskTier }[], rows: readonly AccessRow[]): StoredOverrides {
  // No prototype: a tool named `__proto__` is an own key, as in agent-durable's dict.
  const tool_tiers = Object.create(null) as Record<string, RiskTier>
  const disabled_tools: string[] = []
  for (const tool of [...tools].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
    const source = registrySource(tool.name)
    const row = rows.find((r) => r.source === source && r.item === tool.name)
    const access = toolAccess(tool.risk, row, true)
    if (access === 'off') disabled_tools.push(tool.name)
    else if (access !== tool.risk) tool_tiers[tool.name] = access
  }
  return { tool_tiers, disabled_tools }
}

/** A plugin source's tool rows. No migrated rule: a remote plugin's tiers were set in any direction. */
export function pluginOverrides(source: string, rows: readonly AccessRow[]): { toolTiers: Record<string, RiskTier>; disabledTools: string[] } {
  // No prototype: a tool named `__proto__` is an own key (PluginStore writes it so).
  const toolTiers = Object.create(null) as Record<string, RiskTier>
  const disabledTools: string[] = []
  for (const row of rows) {
    if (row.source !== source || row.item === SOURCE_ITEM || !isToolAccess(row.access)) continue
    if (row.access === 'off') disabledTools.push(row.item)
    else toolTiers[row.item] = row.access
  }
  return { toolTiers, disabledTools: disabledTools.sort() }
}

/**
 * Built-in sources and package servers are on until turned off (a server is offered while
 * its package is approved and enabled, `ai_plugin_packages.enabled`); remote plugins and
 * packages are off until turned on.
 */
export function enabledByDefault(source: string): boolean {
  return source.startsWith('builtin:') || PACKAGE_SERVER_RE.test(source)
}

const PACKAGE_SERVER_RE = /^package:.+\/mcp:.+$/

/** The source's `*` row when it is `on` or `off`, else its default. */
export function sourceEnabled(source: string, rows: readonly AccessRow[]): boolean {
  const row = rows.find((r) => r.source === source && r.item === SOURCE_ITEM)
  if (row?.access === 'on') return true
  if (row?.access === 'off') return false
  return enabledByDefault(source)
}

/**
 * What a caller given no AccessResolver answers: every source and item at its default
 * (resolver.ts's answers without a reader). Here, not there: resolver.ts imports the
 * registry, which the turn modules that need this must not import.
 */
export const DEFAULT_ACCESS = {
  enabled: (source: string): Promise<boolean> => Promise.resolve(enabledByDefault(source)),
  entry: (source: string): Promise<{ enabled: boolean; access: ToolAccess | undefined }> =>
    Promise.resolve({ enabled: enabledByDefault(source), access: undefined }),
  /** resolver.ts `native` with no rows: the default, and `outward` for an off one a package names. */
  native: (_name: string, declared: ToolAccess, packages: readonly string[]): Promise<ToolAccess> =>
    Promise.resolve(declared !== 'off' || packages.length === 0 ? declared : 'outward'),
}

export function isRegistrySource(source: string): boolean {
  return REGISTRY_SOURCES.includes(source)
}
