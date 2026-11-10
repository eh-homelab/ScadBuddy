// Calls that are `outward` by their input, whatever lowers the tool's own tier (#959):
// no Settings override, access tier or InputGuard `allow` verdict may run one unapproved.
// A one-off approval of a host off the asset allowlist IS the approval of the call that
// names it (fetch_asset's `allow_host`), so that call is always parked for a person. A
// call that reads the printer's storage (#4698: get_print_sources with `printer`,
// import_print from a `printer` source) reaches the printer, so it is parked too.
//
// Read by every gate: the harness seam (harness/permissions.ts `decide`) and the
// registry's effective tier (registry.ts `effectiveTool`), which /mcp, confirm_action,
// the durable activities and flows all go through. No imports, so either can use it.

/**
 * The SDK names one of ScadBuddy's own tools `mcp__<server>__<tool>`. Any server's tool
 * of that name counts: pinning only ever raises a tier.
 */
function bareName(toolName: string): string {
  const match = /^mcp__.+__([^_].*)$/.exec(toolName)
  return match?.[1] ?? toolName
}

function field(input: unknown, key: string): unknown {
  return typeof input === 'object' && input !== null && key in input ? (input as Record<string, unknown>)[key] : undefined
}

/**
 * Why this call must be approved by a person whatever the tool's tier, or null. Fails
 * closed: a pinning key whose value is not plainly off counts, since the input may not
 * have been validated yet.
 */
export function pinnedOutwardReason(toolName: string, input: unknown): string | null {
  const name = bareName(toolName)
  if (name === 'fetch_asset' && field(input, 'allow_host') !== undefined) {
    return `${toolName} asks to fetch from a host off the asset allowlist, which needs a human approval in the ScadBuddy UI.`
  }
  if (name === 'get_print_sources' && ![undefined, false].includes(field(input, 'printer') as undefined | false)) {
    return `${toolName} asks to read the printer's storage, which reaches the printer and needs a human approval in the ScadBuddy UI.`
  }
  if (name === 'import_print') {
    const source = field(input, 'source')
    if (typeof source !== 'object' || source === null || field(source, 'kind') !== 'archive' && field(source, 'kind') !== 'archive_source' && field(source, 'kind') !== 'library') {
      return `${toolName} asks to read the print's file from the printer, which reaches the printer and needs a human approval in the ScadBuddy UI.`
    }
  }
  return null
}

/** Whether this call must be approved by a person whatever the tool's tier (`pinnedOutwardReason`). */
export function pinnedOutward(toolName: string, input: unknown): boolean {
  return pinnedOutwardReason(toolName, input) !== null
}
