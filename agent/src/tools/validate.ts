import type { components } from '../api/schema.js'

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

const HEX_COLOUR = /^#?(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i

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
      return typeof value === 'string' && (HEX_COLOUR.test(value) || /^[a-z]+$/i.test(value))
        ? undefined
        : 'must be a colour: a #rrggbb hex value or a colour name'
    case 'string':
    case 'font':
    case 'file': {
      if (typeof value !== 'string') return 'must be a string'
      if (p.max_length !== null && p.max_length !== undefined && value.length > p.max_length) {
        return `must be at most ${p.max_length} characters`
      }
      if (p.type === 'file' && value !== '' && !/^[0-9a-f]{64}$/.test(value)) {
        return 'must be an asset id from upload_asset (a SHA-256), or "" for none'
      }
      return undefined
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
