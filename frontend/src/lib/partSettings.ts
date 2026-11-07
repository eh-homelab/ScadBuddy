import type { PartSettings, PartSettingsMap } from '../api/types'

/**
 * #5627 — the slicer settings a print can set on the parts of one filament slot
 * (print-flow spec §4 "Settings per part (#5571)", `backend/scadbuddy/bambuddy/part_settings.py`).
 * The bounds are the backend's, Bambu Studio v02.08.02.61's `PrintConfig.cpp`.
 */
export type PartSettingKey = keyof PartSettings
type NumberKey = 'top_shell_layers' | 'bottom_shell_layers' | 'wall_loops' | 'sparse_infill_density'
type ChoiceKey = 'sparse_infill_pattern' | 'ironing_type'

export const NUMBER_SETTINGS: readonly { key: NumberKey; label: string; max: number; unit?: string }[] = [
  { key: 'top_shell_layers', label: 'Top shell layers', max: 1000 },
  { key: 'bottom_shell_layers', label: 'Bottom shell layers', max: 1000 },
  { key: 'wall_loops', label: 'Wall loops', max: 1000 },
  { key: 'sparse_infill_density', label: 'Sparse infill density', max: 100, unit: '%' },
]

/** `sparse_infill_pattern`'s `enum_values` (PrintConfig.cpp lines 3107-3128), as the API takes them. */
const PATTERNS = [
  'concentric',
  'zig-zag',
  'grid',
  'line',
  'cubic',
  'triangles',
  'tri-hexagon',
  'gyroid',
  'honeycomb',
  'adaptivecubic',
  'alignedrectilinear',
  '3dhoneycomb',
  'hilbertcurve',
  'archimedeanchords',
  'octagramspiral',
  'supportcubic',
  'lightning',
  'crosshatch',
  'zigzag',
  'crosszag',
  'lockedzag',
  '2dlattice',
] as const satisfies readonly NonNullable<PartSettings['sparse_infill_pattern']>[]

/** `ironing_type`'s `enum_values` (PrintConfig.cpp lines 4075-4078). */
const IRONING = ['no ironing', 'top', 'topmost', 'solid'] as const satisfies readonly NonNullable<
  PartSettings['ironing_type']
>[]

export const CHOICE_SETTINGS: readonly { key: ChoiceKey; label: string; values: readonly string[] }[] = [
  { key: 'sparse_infill_pattern', label: 'Sparse infill pattern', values: PATTERNS },
  { key: 'ironing_type', label: 'Ironing type', values: IRONING },
]

/** One slot's settings with only the ones set: an unset one is the process's. */
function setOnly(settings: PartSettings | undefined): PartSettings {
  return Object.fromEntries(
    Object.entries(settings ?? {}).filter(([, value]) => value !== null && value !== undefined),
  ) as PartSettings
}

/** How many settings `settings` sets. */
export function changedCount(settings: PartSettings | undefined): number {
  return Object.keys(setOnly(settings)).length
}

/**
 * What a request sends: each slot that sets something, with only what it sets, in slot
 * order; `null` when nothing is set, so a request that sets nothing sends no field.
 */
export function sentPartSettings(value: PartSettingsMap | null | undefined): PartSettingsMap | null {
  const kept = Object.entries(value ?? {})
    .map(([slot, settings]) => [slot, setOnly(settings)] as const)
    .filter(([, settings]) => Object.keys(settings).length > 0)
    .sort(([a], [b]) => Number(a) - Number(b))
  return kept.length > 0 ? Object.fromEntries(kept) : null
}

/** Whether two maps set the same, slots that set nothing aside. */
export function samePartSettings(a: PartSettingsMap | null | undefined, b: PartSettingsMap | null | undefined): boolean {
  return JSON.stringify(sortedKeys(sentPartSettings(a))) === JSON.stringify(sortedKeys(sentPartSettings(b)))
}

function sortedKeys(value: PartSettingsMap | null): unknown {
  if (value === null) return null
  return Object.fromEntries(
    Object.entries(value).map(([slot, settings]) => [
      slot,
      Object.fromEntries(Object.entries(settings).sort(([a], [b]) => a.localeCompare(b))),
    ]),
  )
}

/**
 * `settings` with `key` set to `value`. A value of `null`, or the process's own, is
 * unset: only a setting changed from the process is sent.
 */
export function withSetting<K extends PartSettingKey>(
  settings: PartSettings | undefined,
  key: K,
  value: PartSettings[K] | null,
  processValue: PartSettings[K] | null | undefined,
): PartSettings {
  const next = { ...setOnly(settings) }
  if (value === null || value === undefined || value === processValue) delete next[key]
  else next[key] = value
  return next
}
