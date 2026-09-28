import type { components } from '../api/schema.js'
import { OPENSCAD_COLOUR_NAMES } from './colours.js'

// Checking a parameter set against a model's customizer schema
// (`backend/scadbuddy/render/schema.py` builds the schema; `// color` and
// `// font` are overlaid there, which is why they arrive typed). The backend
// has no validate route, so `validate_params` does it here, before a render
// spends OpenSCAD time on values the customizer would never produce.

type Schema = components['schemas']['CustomizerSchema']
type Parameter = components['schemas']['Parameter']
type Value = boolean | number | string

export type ValidationIssue = { param: string; problem: string }

export type ValidationReport = {
  valid: boolean
  issues: ValidationIssue[]
  /** Every parameter's effective value: the given one, or the default. */
  effective: Record<string, Value | null>
}

// OpenSCAD's hex forms, `#rgb`, `#rgba`, `#rrggbb` and `#rrggbbaa` (manual, as in
// colours.ts), as backend/scadbuddy/render/colours.py `_HEX_RE` checks them.
const HEX_COLOUR = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i
const ASSET_ID = /^[0-9a-f]{64}$/

/** What backend/scadbuddy/render/colours.py `colour_hex` reads as a colour (not `None`). */
export function isOpenScadColour(value: string): boolean {
  const trimmed = value.trim()
  return trimmed.startsWith('#') ? HEX_COLOUR.test(trimmed) : OPENSCAD_COLOUR_NAMES.has(trimmed.toLowerCase())
}

/**
 * A `// file` parameter's accepted values, mirroring the backend exactly:
 * backend/scadbuddy/library/assets.py `file_assets` takes "" or the
 * parameter's own `initial` as they are, then one of the model's sample
 * files it accepts (`sample_files`; the schema lists them per parameter as
 * `samples`, filtered by `accept` in `with_samples`), then the id of an
 * uploaded asset (`ASSET_ID_PATTERN`, `^[0-9a-f]{64}$`; that it exists and is
 * of an accepted kind only the backend's store can say). render/runner.py
 * `format_scad_value` additionally requires anything but "" and `initial` to
 * be a bare filename (`is_bare_filename`), which samples and asset ids are.
 */
function fileProblem(p: Parameter, value: string): string | undefined {
  if (value === '' || value === p.initial) return undefined
  const samples = p.samples ?? []
  if (samples.includes(value)) return undefined
  if (ASSET_ID.test(value)) return undefined
  return (
    'must be "" (none), the default' +
    (typeof p.initial === 'string' && p.initial !== '' ? ` (${JSON.stringify(p.initial)})` : '') +
    (samples.length ? `, a sample file (${samples.map((n) => JSON.stringify(n)).join(', ')})` : '') +
    ', or an asset id from upload_asset'
  )
}

function checkOne(p: Parameter, value: Value): string | undefined {
  switch (p.type) {
    case 'boolean':
      return typeof value === 'boolean' ? undefined : 'must be true or false'
    case 'integer':
    case 'number':
    case 'slider': {
      if (typeof value !== 'number' || !Number.isFinite(value)) return 'must be a number'
      if (p.type === 'integer' && !Number.isInteger(value)) return 'must be a whole number'
      if (p.min !== null && p.min !== undefined && value < p.min) return `must be at least ${p.min}`
      if (p.max !== null && p.max !== undefined && value > p.max) return `must be at most ${p.max}`
      return undefined
    }
    case 'select': {
      const allowed = (p.options ?? []).map((o) => o.value)
      return allowed.some((a) => a === value)
        ? undefined
        : `must be one of ${allowed.map((a) => JSON.stringify(a)).join(', ')}`
    }
    case 'color':
      return typeof value === 'string' && isOpenScadColour(value)
        ? undefined
        : 'must be a colour: #rgb, #rgba, #rrggbb or #rrggbbaa, or an SVG colour name OpenSCAD knows (e.g. "red")'
    case 'string':
    case 'font':
    case 'file': {
      if (typeof value !== 'string') return 'must be a string'
      if (p.max_length !== null && p.max_length !== undefined && value.length > p.max_length) {
        return `must be at most ${p.max_length} characters`
      }
      return p.type === 'file' ? fileProblem(p, value) : undefined
    }
  }
}

export function validateParams(schema: Schema, params: Record<string, Value>): ValidationReport {
  const byName = new Map((schema.parameters ?? []).map((p) => [p.name, p]))
  const issues: ValidationIssue[] = []
  for (const [name, value] of Object.entries(params)) {
    const p = byName.get(name)
    if (!p) {
      issues.push({ param: name, problem: 'is not a parameter of this model' })
      continue
    }
    const problem = checkOne(p, value)
    if (problem) issues.push({ param: name, problem })
  }
  const effective: Record<string, Value | null> = {}
  for (const p of schema.parameters ?? []) effective[p.name] = params[p.name] ?? p.initial ?? null
  return { valid: issues.length === 0, issues, effective }
}
