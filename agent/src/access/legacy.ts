import { RISK_TIERS, type RiskTier } from '../harness/permissions.js'
import { type AccessRow, BUILTIN_SOURCES } from './model.js'
import { registryOverrides, sourceEnabled } from './resolve.js'

// #2171 (#4756), rolling deploy: the ai_settings keys an older replica still reads and writes.
// The new code writes them from ai_access's rows (access/store.ts), in the same transaction
// and before its ai_access write; migration 20261027T0514Z_access_settings_mirror.sql mirrors
// an older replica's write of them the other way. Drop both next release (#4828).

export const LEGACY_SOURCES: readonly string[] = Object.values(BUILTIN_SOURCES)

const CAMERA = 'get_printer_camera'
const REGISTRY_KEY = 'builtin_tools.scadbuddy'

export type RegistryTool = { readonly name: string; readonly risk: RiskTier }

/**
 * The old keys of the `touched` sources as `rows` give them, each with what the old code
 * reads when the key is absent. `builtin_tools.scadbuddy` holds raised tiers only (the old
 * code ignored a lowered one) and leaves the camera to `printer_camera_enabled`; without
 * `tools` the registry's keys are not written.
 */
export function legacySettings(
  rows: readonly AccessRow[],
  touched: ReadonlySet<string>,
  tools: readonly RegistryTool[] | undefined,
): { key: string; value: unknown; absent: unknown }[] {
  const out: { key: string; value: unknown; absent: unknown }[] = []
  if (tools && (touched.has(BUILTIN_SOURCES.scadbuddy) || touched.has(BUILTIN_SOURCES.browser))) {
    const overrides = registryOverrides(tools, rows)
    const risk = new Map(tools.map((t) => [t.name, t.risk]))
    const tool_tiers: Record<string, RiskTier> = {}
    for (const [tool, tier] of Object.entries(overrides.tool_tiers)) {
      if (RISK_TIERS.indexOf(tier) > RISK_TIERS.indexOf(risk.get(tool)!)) tool_tiers[tool] = tier
    }
    const disabled_tools = overrides.disabled_tools.filter((t) => t !== CAMERA)
    out.push({ key: REGISTRY_KEY, value: { tool_tiers, disabled_tools }, absent: { tool_tiers: {}, disabled_tools: [] } })
    if (risk.has(CAMERA)) out.push({ key: 'printer_camera_enabled', value: !overrides.disabled_tools.includes(CAMERA), absent: true })
  }
  if (touched.has(BUILTIN_SOURCES.http)) {
    out.push({ key: 'http_request_enabled', value: sourceEnabled(BUILTIN_SOURCES.http, rows), absent: true })
  }
  if (touched.has(BUILTIN_SOURCES.ownPlugin)) {
    out.push({ key: 'scadbuddy_plugin_enabled', value: sourceEnabled(BUILTIN_SOURCES.ownPlugin, rows), absent: true })
  }
  return out
}
