// Calls that are `outward` by their input, whatever lowers the tool's own tier (#959):
// no Settings override, access tier or InputGuard `allow` verdict may run one unapproved.
// A one-off approval of a host off the asset allowlist IS the approval of the call that
// names it (fetch_asset's `allow_host`), so that call is always parked for a person. A
// call that reads the printer's storage (#4698: get_print_sources with `printer`,
// import_print from a `printer` source, #6916: create_model_from_mesh from a print's
// `printer` source) reaches the printer, so it is parked too. An http_request that names
// `allow_private_host` (#8178) asks to read a private or loopback host, and the approval of
// that call is what lets it through, so it is parked too, whatever its method.
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
  if (name === 'http_request' && field(input, 'allow_private_host') !== undefined) {
    return `${toolName} asks to reach a private or loopback host (allow_private_host), which needs a human approval in the ScadBuddy UI.`
  }
  if (name === 'get_print_sources' && ![undefined, false].includes(field(input, 'printer') as undefined | false)) {
    return `${toolName} asks to read the printer's storage, which reaches the printer and needs a human approval in the ScadBuddy UI.`
  }
  if ((name === 'import_print' && readsPrinter(field(input, 'source'))) || (name === 'create_model_from_mesh' && field(input, 'archive_source') !== undefined && readsPrinter(field(input, 'archive_source')))) {
    return `${toolName} asks to read the print's file from the printer, which reaches the printer and needs a human approval in the ScadBuddy UI.`
  }
  return null
}

/** The print-file places that never reach the printer (`get_print_sources`' kinds but `printer`). */
const OFF_PRINTER_KINDS: readonly unknown[] = ['archive', 'archive_source', 'library']

/**
 * Whether a print's file `source` (import_print's `source`, create_model_from_mesh's
 * `archive_source`) reads the printer. Fails closed: anything but one of the three kinds
 * that stay off the printer counts.
 */
function readsPrinter(source: unknown): boolean {
  return typeof source !== 'object' || source === null || !OFF_PRINTER_KINDS.includes(field(source, 'kind'))
}

/** Whether this call must be approved by a person whatever the tool's tier (`pinnedOutwardReason`). */
export function pinnedOutward(toolName: string, input: unknown): boolean {
  return pinnedOutwardReason(toolName, input) !== null
}

/**
 * Whether only a person's own approval of this very call may run it, never a run grant
 * or another decider (#959, #8178): a one-off host off the asset allowlist, and a read of
 * a private or loopback host. Fails closed as `pinnedOutwardReason` does.
 */
export function needsPersonsApproval(toolName: string, input: unknown): boolean {
  const name = bareName(toolName)
  return (name === 'fetch_asset' && field(input, 'allow_host') !== undefined) || (name === 'http_request' && field(input, 'allow_private_host') !== undefined)
}
