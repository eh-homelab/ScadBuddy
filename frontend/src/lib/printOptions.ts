/**
 * Print-option merging and presentation, shared by the send bar and (from #86) the
 * print picker.
 *
 * The merge here mirrors `scadbuddy.bambuddy.options.resolve` exactly — global, then
 * per-printer, then per-model, then this send — because the value the UI shows as
 * effective has to be the value the server will send. `null`/`undefined` means "not set
 * at this scope" and never clears a scope below it.
 */
import type { OptionScope, PrintOptions } from '../api/types'

export type OptionName = keyof PrintOptions
export type OptionValue = boolean | number | string | null
export type OptionKind = 'boolean' | 'calibration' | 'preheat' | 'number'

export interface OptionSpec {
  name: OptionName
  label: string
  kind: OptionKind
  hint?: string
  /** For a number control, so an out-of-range value is caught before the round trip. */
  min?: number
  max?: number
}

/**
 * Every option field of Bambuddy's `PrintQueueItemCreate` that #88 covers, in the order
 * the issue lists them. The names are Bambuddy's, not ScadBuddy's — the backend passes
 * them straight through.
 */
export const PRINT_OPTIONS: readonly OptionSpec[] = [
  { name: 'bed_levelling', label: 'Bed levelling', kind: 'calibration' },
  { name: 'flow_cali', label: 'Flow calibration', kind: 'calibration' },
  { name: 'vibration_cali', label: 'Vibration calibration', kind: 'boolean' },
  { name: 'nozzle_offset_cali', label: 'Nozzle offset calibration', kind: 'calibration' },
  { name: 'layer_inspect', label: 'First-layer inspection', kind: 'boolean' },
  { name: 'timelapse', label: 'Timelapse', kind: 'boolean' },
  { name: 'use_ams', label: 'Use the AMS', kind: 'boolean' },
  {
    name: 'quantity',
    label: 'Quantity',
    kind: 'number',
    // The Copies box above is the same value, not a competing one.
    hint: 'Copies above',
    min: 1,
    max: 1000,
  },
  { name: 'manual_start', label: 'Wait for a manual start', kind: 'boolean' },
  { name: 'insert_at_top', label: 'Insert at the top of the queue', kind: 'boolean' },
  { name: 'auto_off_after', label: 'Power off afterwards', kind: 'boolean' },
  // No `project_id`: the Project picker is the one control for it, and a run drops an
  // options `project_id` rather than half-apply it (`print_run.py`), so a field here
  // would be a control that never takes effect (#1047).
  { name: 'preheat_override', label: 'Preheat', kind: 'preheat' },
  {
    name: 'preheat_chamber_target_override',
    label: 'Chamber preheat target',
    kind: 'number',
    hint: '0-65 °C',
    min: 0,
    max: 65,
  },
]

/**
 * `min`/`max`/`step` on a number input are advisory: a browser enforces them on form
 * submission, not on what the user types, and neither the Options disclosure nor the
 * Copies box submits a form. This is what stops an out-of-range *or* fractional value
 * reaching the server, where every one of these fields is an `int` and a Pydantic 422
 * names no field. Rounding matters as much as bounding: `Math.min`/`Math.max` leave
 * `3.5` alone.
 */
export function clampOption(raw: string, spec: Pick<OptionSpec, 'min' | 'max'>): number | null {
  const whole = Math.round(Number(raw))
  // A `type="number"` input reports a lone "-" as its value, and `??` does not fall
  // through `NaN`, so an unguarded one sticks in the field and blanks the control.
  if (!Number.isFinite(whole)) return null
  const lower = spec.min === undefined ? whole : Math.max(spec.min, whole)
  return spec.max === undefined ? lower : Math.min(spec.max, lower)
}

export const CALIBRATION_CHOICES = ['off', 'on', 'auto'] as const
export const PREHEAT_CHOICES = ['inherit', 'on', 'off'] as const

/** One layer of the merge, most general first. */
export interface OptionLayer {
  scope: OptionScope | 'request'
  options: PrintOptions | undefined
}

export function isSet(value: OptionValue | undefined): value is OptionValue {
  return value !== null && value !== undefined
}

/** Later layers win field by field; an unset field never clears an earlier one. */
export function resolveOptions(...layers: (PrintOptions | undefined | null)[]): PrintOptions {
  const merged: Record<string, OptionValue> = {}
  for (const layer of layers) {
    if (!layer) continue
    for (const [name, value] of Object.entries(layer)) {
      if (isSet(value as OptionValue | undefined)) merged[name] = value as OptionValue
    }
  }
  return merged as PrintOptions
}

export function optionValue(options: PrintOptions | undefined, name: OptionName): OptionValue {
  const value = options?.[name]
  return isSet(value as OptionValue | undefined) ? (value as OptionValue) : null
}

/** Which layer the effective value came from, or `null` when nothing set it. */
export function effectiveScope(layers: OptionLayer[], name: OptionName): OptionLayer['scope'] | null {
  let found: OptionLayer['scope'] | null = null
  for (const layer of layers) {
    if (isSet(layer.options?.[name] as OptionValue | undefined)) found = layer.scope
  }
  return found
}

export const SCOPE_LABELS: Record<OptionLayer['scope'], string> = {
  global: 'everywhere',
  printer: 'this printer',
  model: 'this model',
  request: 'this send',
}

export function formatOption(kind: OptionKind, value: OptionValue): string {
  if (!isSet(value)) return 'not set'
  if (kind === 'boolean') return value ? 'On' : 'Off'
  if (typeof value === 'string') return value.charAt(0).toUpperCase() + value.slice(1)
  return String(value)
}

/** Whether the effective value differs from what Bambuddy would have done. */
export function isNonDefault(
  name: OptionName,
  effective: PrintOptions,
  defaults: PrintOptions,
): boolean {
  const value = optionValue(effective, name)
  if (!isSet(value)) return false
  return value !== optionValue(defaults, name)
}
