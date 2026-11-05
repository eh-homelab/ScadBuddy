// Calls that are `outward` by their input, whatever lowers the tool's own tier (#959):
// no Settings override, access tier or InputGuard `allow` verdict may run one unapproved.
// A one-off approval of a host off the asset allowlist IS the approval of the call that
// names it (fetch_asset's `allow_host`), so that call is always parked for a person.
//
// Read by every gate: the harness seam (harness/permissions.ts `decide`) and the
// registry's effective tier (registry.ts `effectiveTool`), which /mcp, confirm_action,
// the durable activities and flows all go through. No imports, so either can use it.

/**
 * The SDK names one of ScadBuddy's own tools `mcp__<server>__<tool>`. Any server's
 * `fetch_asset` counts: pinning only ever raises a tier.
 */
function isFetchAsset(toolName: string): boolean {
  return toolName === 'fetch_asset' || (toolName.startsWith('mcp__') && toolName.endsWith('__fetch_asset'))
}

/**
 * Whether this call must be approved by a person whatever the tool's tier. Fails closed:
 * any `allow_host` key on a fetch_asset input counts, whatever its value, since the input
 * may not have been validated yet.
 */
export function pinnedOutward(toolName: string, input: unknown): boolean {
  if (!isFetchAsset(toolName)) return false
  return typeof input === 'object' && input !== null && 'allow_host' in input && (input as { allow_host?: unknown }).allow_host !== undefined
}
