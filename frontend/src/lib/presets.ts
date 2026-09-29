import type { CustomizerSchema, ParamPreset, ParamValue } from '../api/types'
import { allParams, defaultValues, diffFromDefaults, type ParamValues } from './params'

export interface AppliedPreset {
  /** The whole set of values on screen once the preset is applied. */
  values: ParamValues
  /** The preset's parameters this template no longer has, which were left out. */
  skipped: string[]
}

/**
 * A preset holds only the values it sets, so it is applied over the template's
 * defaults: a default the template changes later still reaches every preset that never
 * touched it. A value for a parameter the template has since dropped is skipped rather
 * than sent — the render would refuse it.
 */
export function applyPreset(schema: CustomizerSchema, preset: ParamPreset): AppliedPreset {
  const known = new Set(allParams(schema).map((param) => param.name))
  const values: ParamValues = defaultValues(schema)
  const skipped: string[] = []
  for (const [name, value] of Object.entries(preset.params)) {
    if (known.has(name)) values[name] = value
    else skipped.push(name)
  }
  return { values, skipped }
}

/** What a preset saved from `values` holds: only the values that differ from the defaults. */
export function presetParams(
  schema: CustomizerSchema,
  values: ParamValues,
): Record<string, ParamValue> {
  return Object.fromEntries(diffFromDefaults(schema, values).map((diff) => [diff.name, diff.value]))
}

/** The server's bounds on a preset's details (#327, `library/presets.py`). */
export const MAX_PRESET_DESCRIPTION = 2000
export const MAX_PRESET_TAGS = 20
export const MAX_PRESET_TAG = 40

/**
 * A tag's key for "the same tag", as close to the server's `str.casefold()` as JS
 * gets: upper then lower folds `ß` with `SS` and `ſ` with `s`, which `toLowerCase()`
 * alone does not. A fold it still misses only makes the client stricter.
 */
export function foldTag(tag: string): string {
  return tag.toUpperCase().toLowerCase()
}

/** A tag's length as the server counts it: in code points, not UTF-16 units. */
export function tagLength(tag: string): number {
  return [...tag].length
}

/**
 * Why this description would be refused, in words, or null. Its length is counted as
 * the server counts it, in code points -- the same count as a tag's, where a
 * `maxLength` on the field would count UTF-16 units and cut an emoji short.
 */
export function presetDescriptionProblem(description: string): string | null {
  return tagLength(description) > MAX_PRESET_DESCRIPTION
    ? `The description is longer than ${MAX_PRESET_DESCRIPTION} characters.`
    : null
}

/**
 * Tags as typed, comma-separated, cleaned as the server cleans them: trimmed, inner
 * whitespace collapsed, blanks dropped, each kept once ignoring case. The server
 * refuses a comma in a tag, so the split never breaks a stored tag in two.
 */
export function parsePresetTags(text: string): string[] {
  const seen = new Set<string>()
  const tags: string[] = []
  for (const raw of text.split(',')) {
    const tag = raw.trim().replace(/\s+/g, ' ')
    if (tag && !seen.has(foldTag(tag))) {
      seen.add(foldTag(tag))
      tags.push(tag)
    }
  }
  return tags
}

/**
 * Why these tags would be refused, in words, or null. Checked before a save: the
 * server's own refusal of a body is a generic "did not match the expected shape".
 */
export function presetTagsProblem(tags: readonly string[]): string | null {
  if (tags.length > MAX_PRESET_TAGS) return `At most ${MAX_PRESET_TAGS} tags.`
  const long = tags.find((tag) => tagLength(tag) > MAX_PRESET_TAG)
  return long ? `“${long}” is longer than ${MAX_PRESET_TAG} characters.` : null
}
