import type { ProcessOverrides } from '../api/types'

/**
 * #2998 — any Bambu Studio process setting, by key (print-flow spec §4 "Any slicer
 * setting"). No key or value is refused here or by the server: the slicer judges each.
 */
export type SettingValue = ProcessOverrides[string]

/** A value as a Bambu config writes it: a boolean `1`/`0`, a list joined with commas. */
export function settingText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'boolean') return value ? '1' : '0'
  if (Array.isArray(value)) {
    const items = value.map(settingText)
    // A per-extruder option with one value on every side reads as that value.
    return items.length > 0 && items.every((item) => item === items[0]) ? (items[0] ?? '') : items.join(',')
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/** `value` without unset entries, keys sorted; `null` when nothing is set, so none is sent. */
export function sentOverrides(value: Record<string, unknown> | null | undefined): ProcessOverrides | null {
  const kept = Object.entries(value ?? {})
    .filter(([, setting]) => setting !== null && setting !== undefined)
    .sort(([a], [b]) => a.localeCompare(b)) as [string, SettingValue][]
  return kept.length > 0 ? Object.fromEntries(kept) : null
}

/** Whether two sets of settings set the same, compared by what each writes. */
export function sameOverrides(
  a: Record<string, unknown> | null | undefined,
  b: Record<string, unknown> | null | undefined,
): boolean {
  const text = (value: Record<string, unknown> | null | undefined) =>
    JSON.stringify(Object.entries(sentOverrides(value) ?? {}).map(([key, setting]) => [key, settingText(setting)]))
  return text(a) === text(b)
}

/**
 * `settings` with `key` set to the typed `text`: blank, or the text the setting already
 * prints with (`baseline`), is unset, so only a change is sent.
 */
export function withText<T extends Record<string, unknown>>(
  settings: T | undefined,
  key: string,
  text: string | null,
  baseline: unknown,
): T {
  const next: Record<string, unknown> = { ...(settings ?? {}) }
  if (text === null || text === '' || (baseline !== undefined && text === settingText(baseline))) delete next[key]
  else next[key] = text
  return next as T
}

/** How many settings `settings` sets. */
export function changedCount(settings: Record<string, unknown> | null | undefined): number {
  return Object.values(settings ?? {}).filter((value) => value !== null && value !== undefined).length
}
