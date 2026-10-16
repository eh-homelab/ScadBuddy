import type { OutputPlate } from '../api/types'

/**
 * #3652 — the plates chosen to print: every one, or a set of them in plate order. A set
 * may be empty while the user changes it; nothing prints until one is chosen.
 */
export type PlateChoice = 'all' | readonly number[]

/** The one plate chosen, or `null` for every plate or several. */
export function singlePlate(choice: PlateChoice): number | null {
  return choice !== 'all' && choice.length === 1 ? (choice[0] ?? null) : null
}

/** The first plate that prints: what a one-plate preview shows. */
export function firstPlate(choice: PlateChoice): number {
  return choice === 'all' ? 1 : (choice[0] ?? 1)
}

/** A stable string for the choice, for effect keys and React keys. */
export function plateKey(choice: PlateChoice): string {
  return choice === 'all' ? 'all' : choice.join(',')
}

/** What a print, check or analysis request says of the choice. */
export function plateFields(choice: PlateChoice): { plate_id: number; all_plates: boolean; plate_ids?: number[] } {
  if (choice === 'all') return { plate_id: 1, all_plates: true }
  if (choice.length > 1) return { plate_id: choice[0] ?? 1, all_plates: false, plate_ids: [...choice] }
  return { plate_id: choice[0] ?? 1, all_plates: false }
}

/** `request` for plate `index` alone: what the preview pane slices of one plate. */
export function withPlate<T extends { plate_id?: number; all_plates?: boolean; plate_ids?: number[] | null }>(
  request: T,
  index: number,
): T {
  const next = { ...request, plate_id: index, all_plates: false }
  delete next.plate_ids
  return next
}

/**
 * The choice over the plates offered: one plate (or a list not read yet) is that plate,
 * plates no longer offered drop out, and every offered plate is "all". A set that loses
 * every plate it had falls back to the first one offered.
 */
export function offeredChoice(choice: PlateChoice, offered: readonly number[]): PlateChoice {
  if (offered.length <= 1) return [offered[0] ?? 1]
  if (choice === 'all') return 'all'
  const kept = choice.filter((index) => offered.includes(index))
  if (kept.length === offered.length) return 'all'
  if (kept.length === 0 && choice.length > 0) return [offered[0] ?? 1]
  return kept
}

/** The choice with `index` ticked or unticked. Every offered plate ticked is "all". */
export function togglePlate(choice: PlateChoice, index: number, offered: readonly number[]): PlateChoice {
  const current = choice === 'all' ? offered : choice
  const next = current.includes(index)
    ? current.filter((entry) => entry !== index)
    : [...current, index].sort((a, b) => a - b)
  return offered.length > 1 && next.length === offered.length ? 'all' : next
}

/** Whether plate `index` prints. */
export function plateChosen(choice: PlateChoice, index: number): boolean {
  return choice === 'all' || choice.includes(index)
}

function listed(numbers: readonly number[]): string {
  if (numbers.length <= 2) return numbers.join(' and ')
  return `${numbers.slice(0, -1).join(', ')} and ${numbers[numbers.length - 1]}`
}

/** "All 3 plates", "Plate 2 (1 of 3)", "Plates 1 and 3 (2 of 3)", or "No plate chosen". */
export function platesSummary(choice: PlateChoice, plates: readonly OutputPlate[]): string {
  const count = plates.length
  if (choice === 'all') return `All ${count} plates`
  if (choice.length === 0) return 'No plate chosen'
  const what = choice.length === 1 ? `Plate ${choice[0]}` : `Plates ${listed(choice)}`
  return `${what} (${choice.length} of ${count})`
}
