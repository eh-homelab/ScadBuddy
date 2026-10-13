import type { FlowGrantRule, FlowGrantSpec, FlowGrantView } from '../api/types'

// #2481 (spec 2026-10-11 §3.1) — run grants as the backend checks them (`flows/grants.py`).

export const HOUR = 3_600_000
/** The latest a grant may expire, from now (backend `MAX_EXPIRY`). */
export const MAX_GRANT_MS = 7 * 24 * HOUR
const MAX_RULES = 32

export type HostFn = FlowGrantRule['fn']
export type Bound = Exclude<keyof FlowGrantRule, 'fn' | 'tool'>

/** The functions each bound applies to, as the backend refuses any other use. */
export const BOUNDS: Record<Bound, readonly HostFn[]> = {
  printers: ['queue_print'],
  models: ['queue_print', 'arrange'],
  library_files: ['queue_print'],
  max_prints: ['queue_print'],
  max_filament_g: ['queue_print'],
  max_calls: ['queue_print', 'arrange', 'tool'],
  args: ['tool'],
}

/** What an unbounded rule lets through, said in red (spec §3.1). */
export function unboundedText(rule: Pick<FlowGrantRule, 'fn'>): string {
  if (rule.fn === 'queue_print') return 'No bounds: any printer, any model, any number of prints.'
  if (rule.fn === 'arrange') return 'No bounds: any model, any number of calls.'
  return 'No bounds: any arguments, any number of calls.'
}

/** A rule that sets no bound admits every call of its function. */
export function isUnbounded(rule: FlowGrantRule): boolean {
  return (Object.keys(BOUNDS) as Bound[]).every((bound) => rule[bound] == null)
}

function isPointer(path: string): boolean {
  return path.startsWith('/') && !/~[^01]|~$/.test(path)
}

/**
 * Every problem the backend would refuse `spec` for (`validate_spec`), as of `now`;
 * empty when it may be sent. `outward` are the tools a rule may name.
 */
export function grantProblems(spec: FlowGrantSpec, now: Date, outward: ReadonlySet<string>): string[] {
  const problems: string[] = []
  const rules = spec.rules ?? []
  if (rules.length > MAX_RULES) problems.push(`A grant has at most ${MAX_RULES} rules.`)
  rules.forEach((rule, i) => {
    const at = `Rule ${i + 1}`
    if (rule.fn === 'tool') {
      if (!rule.tool) problems.push(`${at}: a tool rule names its tool.`)
      else if (!outward.has(rule.tool)) problems.push(`${at}: ${rule.tool} is not an outward tool; a grant names only outward tools.`)
    } else if (rule.tool != null) {
      problems.push(`${at}: only a tool rule names a tool.`)
    }
    for (const bound of Object.keys(BOUNDS) as Bound[]) {
      if (rule[bound] != null && !BOUNDS[bound].includes(rule.fn)) problems.push(`${at}: ${bound} does not bound ${rule.fn}.`)
    }
    for (const bound of ['max_prints', 'max_filament_g', 'max_calls'] as const) {
      const value = rule[bound]
      if (value != null && value < 0) problems.push(`${at}: ${bound} is negative.`)
    }
    for (const arg of rule.args ?? []) {
      if (!isPointer(arg.path)) problems.push(`${at}: '${arg.path}' is not a JSON pointer.`)
      if (arg.in.length === 0) problems.push(`${at}: ${arg.path} allows no value.`)
    }
  })
  const expires = new Date(spec.expires_at).getTime()
  if (Number.isNaN(expires)) problems.push('The grant needs an expiry.')
  else if (expires <= now.getTime()) problems.push('The grant expires in the past.')
  else if (expires - now.getTime() > MAX_GRANT_MS) problems.push('The grant expires more than 7 days from now.')
  return problems
}

/** The grant's state as one word: revoked and expired are what an approved grant became. */
export function grantStatus(grant: FlowGrantView): string {
  if (grant.revoked_at) return 'Revoked'
  if (grant.status === 'requested') return 'Requested'
  if (grant.status === 'declined') return 'Declined'
  return grant.in_force ? 'In force' : 'Expired'
}
