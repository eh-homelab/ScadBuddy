import type { Quote, Revision } from '../../api/types'
import type { ParamValues } from '../../lib/params'
import { byCreated } from './price'

/**
 * #4673 PR 4 (order operations spec §5.2) — an item's print setups: its quotes, grouped by
 * the revision each was made of, with what each changed from the accepted one.
 */

/** Why Accept is off on a quote of a revision that is not the item's current one (`accept_quote` refuses it). */
export const OLD_REVISION_TEXT = "Made for an older revision: slice the current one to accept a setup of it."

export interface SetupGroup {
  /** The revision id the quotes name, or null; unique per group, so it keys the group. */
  key: string | null
  /** The revision the quotes were made of; null for quotes that name none, or one not listed. */
  revision: Revision | null
  /** The item's current revision. */
  current: boolean
  quotes: Quote[]
}

/** The quotes grouped by revision: the current one first, then the others newest first, each group's quotes oldest first. */
export function setupGroups(quotes: Quote[], revisions: Revision[], currentRevisionId: string | null | undefined): SetupGroup[] {
  const byId = new Map(revisions.map((revision) => [revision.id, revision]))
  const groups = new Map<string, SetupGroup>()
  for (const quote of [...quotes].sort(byCreated)) {
    const key = quote.revision_id ?? ''
    let group = groups.get(key)
    if (!group) {
      group = {
        key: quote.revision_id ?? null,
        revision: quote.revision_id ? (byId.get(quote.revision_id) ?? null) : null,
        current: !!quote.revision_id && quote.revision_id === currentRevisionId,
        quotes: [],
      }
      groups.set(key, group)
    }
    group.quotes.push(quote)
  }
  const rank = (group: SetupGroup) => (group.current ? Number.MAX_SAFE_INTEGER : (group.revision?.number ?? 0))
  return [...groups.values()].sort((a, b) => rank(b) - rank(a))
}

/** Whether Accept is open on `quote`: a quote of an older revision is refused. */
export function oldRevision(quote: Quote, currentRevisionId: string | null | undefined): boolean {
  return !!quote.revision_id && !!currentRevisionId && quote.revision_id !== currentRevisionId
}

export interface SetupStats {
  plates: number
  seconds: number
  grams: number
}

type PlateRow = { index?: number; print_seconds?: number; filaments?: { used_g?: string | number }[] }

/** A quote's plate count, print time and grams, from its sliced plates. */
export function setupStats(quote: Quote): SetupStats {
  const plates = quote.plates as PlateRow[]
  return {
    plates: plates.length,
    seconds: plates.reduce((sum, plate) => sum + (plate.print_seconds ?? 0), 0),
    grams: plates.reduce((sum, plate) => sum + (plate.filaments ?? []).reduce((g, f) => g + Number(f.used_g ?? 0), 0), 0),
  }
}

/** The plate numbers a quote sliced, for its previews. */
export function setupPlates(quote: Quote): number[] {
  return (quote.plates as PlateRow[]).map((plate) => plate.index).filter((index): index is number => typeof index === 'number')
}

function shown(value: unknown): string {
  if (value === null || value === undefined) return 'none'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

function paramsOf(revision: Revision | undefined): Record<string, unknown> {
  const params = (revision?.reference.inputs as Record<string, unknown> | undefined)?.['params']
  return params && typeof params === 'object' ? (params as Record<string, unknown>) : {}
}

/** The setup's parts the page names, in a quote's `choices` (`orders/quote.py` `print_setup`). */
const SETUP: [string, (choices: Record<string, unknown>) => unknown][] = [
  ['printer', (c) => c['printer'] ?? c['printer_id']],
  ['quality', (c) => (c['choices'] as Record<string, unknown> | null | undefined)?.['tier']],
  ['process', (c) => (c['choices'] as Record<string, unknown> | null | undefined)?.['process_name']],
  ['nozzles', (c) => (c['choices'] as Record<string, unknown> | null | undefined)?.['nozzles']],
  ['prime tower', (c) => c['prime_tower']],
  ['supports', (c) => c['supports']],
  ['print sequence', (c) => c['print_sequence']],
]

/**
 * What `quote` changed from `accepted`: each input that differs between their revisions,
 * then each part of the print setup, as `name old → new`. Empty for the accepted one,
 * or when there is none to compare with.
 */
export function setupChanges(quote: Quote, accepted: Quote | undefined, revisions: Revision[]): string[] {
  if (!accepted || accepted.id === quote.id) return []
  const changes: string[] = []
  if (quote.revision_id !== accepted.revision_id) {
    const before = paramsOf(revisions.find((r) => r.id === accepted.revision_id))
    const after = paramsOf(revisions.find((r) => r.id === quote.revision_id))
    for (const name of [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()) {
      if (shown(before[name]) !== shown(after[name])) changes.push(`${name} ${shown(before[name])} → ${shown(after[name])}`)
    }
  }
  const was = (accepted.choices ?? {}) as Record<string, unknown>
  const now = (quote.choices ?? {}) as Record<string, unknown>
  if (accepted.choices && quote.choices) {
    for (const [name, read] of SETUP) {
      if (shown(read(was)) !== shown(read(now))) changes.push(`${name} ${shown(read(was))} → ${shown(read(now))}`)
    }
  }
  return changes
}

/** Whether two parameter sets hold the same values, a vector compared by its items. */
function sameParams(a: ParamValues, b: ParamValues): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)])
  return [...keys].every((key) => JSON.stringify(a[key]) === JSON.stringify(b[key]))
}

/**
 * The inputs a slice sends for `edits` over the item's `own` parameters, or null when they
 * change nothing: compared against the item's values over the form's defaults, so a
 * parameter the item never set, put back to its default, is no change (#5797 review).
 */
export function inputsToSend(
  inputs: Record<string, unknown>,
  defaults: ParamValues,
  own: ParamValues,
  edits: ParamValues,
): Record<string, unknown> | null {
  const base = { ...defaults, ...own }
  return sameParams({ ...base, ...edits }, base) ? null : { ...inputs, params: { ...own, ...edits } }
}

/**
 * A print setups group's name: its revision's number, or the first 8 characters of its id
 * for a revision the item's list lacks, and whether it is the current one; "No revision"
 * for quotes that name none.
 */
export function groupName(group: SetupGroup): string {
  if (!group.key) return 'No revision'
  const name = group.revision ? String(group.revision.number) : group.key.slice(0, 8)
  return `Revision ${name}${group.current ? ' (current)' : ''}`
}
