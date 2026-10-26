import { RISK_TIERS, type RiskTier } from '../harness/permissions.js'
import { type AccessRow, BUILTIN_SOURCES, isToolAccess, MIGRATED_BY, registrySource, SOURCE_ITEM, type StoredOverrides, type ToolAccess } from './model.js'

// The resolution rules of the access model (#2171, spec §4.1–§4.3): pure, no I/O.
// agent-durable applies the same rules; test/fixtures/access-vectors.json pins the two together.

const REGISTRY_SOURCES: readonly string[] = [BUILTIN_SOURCES.scadbuddy, BUILTIN_SOURCES.browser]

/**
 * A tool's effective access: the row's when it holds one, else the declared tier. With
 * `migratedRaisesOnly` (a registry source), a row the migration copied counts only when it is
 * `off` or above `declared`: the old registry overrides could only raise.
 */
export function toolAccess(declared: RiskTier, row: AccessRow | undefined, migratedRaisesOnly: boolean): ToolAccess {
  if (row === undefined || !isToolAccess(row.access)) return declared
  if (migratedRaisesOnly && row.updated_by === MIGRATED_BY && row.access !== 'off') {
    return RISK_TIERS.indexOf(row.access) > RISK_TIERS.indexOf(declared) ? row.access : declared
  }
  return row.access
}

/** The registry tools' overrides: every effective tier that differs from the tool's own, and every `off`. */
export function registryOverrides(tools: readonly { name: string; risk: RiskTier }[], rows: readonly AccessRow[]): StoredOverrides {
  const tool_tiers: Record<string, RiskTier> = {}
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
  const toolTiers: Record<string, RiskTier> = {}
  const disabledTools: string[] = []
  for (const row of rows) {
    if (row.source !== source || row.item === SOURCE_ITEM || !isToolAccess(row.access)) continue
    if (row.access === 'off') disabledTools.push(row.item)
    else toolTiers[row.item] = row.access
  }
  return { toolTiers, disabledTools: disabledTools.sort() }
}

/** Built-in sources are on until turned off; remote plugins and packages are off until turned on. */
export function enabledByDefault(source: string): boolean {
  return source.startsWith('builtin:')
}

/** The source's `*` row when it is `on` or `off`, else its default. */
export function sourceEnabled(source: string, rows: readonly AccessRow[]): boolean {
  const row = rows.find((r) => r.source === source && r.item === SOURCE_ITEM)
  if (row?.access === 'on') return true
  if (row?.access === 'off') return false
  return enabledByDefault(source)
}

export function isRegistrySource(source: string): boolean {
  return REGISTRY_SOURCES.includes(source)
}
