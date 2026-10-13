/**
 * #2483 — a flow's typed inputs: its inputs schema (`inputs.json` beside its script, a
 * closed subset of JSON Schema the backend checks, `flows/inputs.py`), as the Start run
 * form draws it and the editor reads it. The backend checks the values again when the
 * run starts and fills in the defaults; this only turns the form's text into JSON.
 */

export type InputScalar = 'string' | 'integer' | 'number' | 'boolean'
export type InputType = InputScalar | 'array'

export interface InputField {
  name: string
  type: InputType
  /** An array's item type. */
  itemType?: InputScalar
  /** Its `title`, else its name. */
  label: string
  description?: string
  required: boolean
  default?: unknown
  enum?: (string | number)[]
  minimum?: number
  maximum?: number
}

/** What the form holds: text per input, a checkbox's boolean. */
export type InputValues = Record<string, string | boolean>

const SCALARS = new Set(['string', 'integer', 'number', 'boolean'])

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

/** The schema's inputs, in its order; none for a flow without one. */
export function inputFields(schema: unknown): InputField[] {
  if (!isObject(schema) || !isObject(schema['properties'])) return []
  const required = new Set(Array.isArray(schema['required']) ? (schema['required'] as string[]) : [])
  return Object.entries(schema['properties']).flatMap(([name, prop]): InputField[] => {
    if (!isObject(prop)) return []
    const type = prop['type']
    if (typeof type !== 'string' || !(SCALARS.has(type) || type === 'array')) return []
    const items = prop['items']
    const itemType = isObject(items) && typeof items['type'] === 'string' ? (items['type'] as InputScalar) : undefined
    return [
      {
        name,
        type: type as InputType,
        ...(type === 'array' ? { itemType: itemType ?? 'string' } : {}),
        label: typeof prop['title'] === 'string' && prop['title'] ? prop['title'] : name,
        ...(typeof prop['description'] === 'string' ? { description: prop['description'] } : {}),
        required: required.has(name),
        ...('default' in prop ? { default: prop['default'] } : {}),
        ...(Array.isArray(prop['enum']) ? { enum: prop['enum'] as (string | number)[] } : {}),
        ...(typeof prop['minimum'] === 'number' ? { minimum: prop['minimum'] } : {}),
        ...(typeof prop['maximum'] === 'number' ? { maximum: prop['maximum'] } : {}),
      },
    ]
  })
}

/** The form at each input's default: an array one item per line. */
export function initialValues(fields: InputField[]): InputValues {
  return Object.fromEntries(
    fields.map((field) => {
      if (field.type === 'boolean') return [field.name, field.default === true]
      if (field.default === undefined || field.default === null) return [field.name, '']
      if (Array.isArray(field.default)) return [field.name, field.default.map(String).join('\n')]
      return [field.name, String(field.default)]
    }),
  )
}

function scalar(type: InputScalar, text: string): { value?: unknown; error?: string } {
  if (type === 'integer') {
    const value = Number(text)
    return text.trim() !== '' && Number.isInteger(value) ? { value } : { error: 'a whole number' }
  }
  if (type === 'number') {
    const value = Number(text)
    return text.trim() !== '' && Number.isFinite(value) ? { value } : { error: 'a number' }
  }
  if (type === 'boolean') {
    if (text === 'true' || text === 'false') return { value: text === 'true' }
    return { error: 'true or false' }
  }
  return { value: text }
}

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)

/**
 * The run's inputs from the form, and what does not parse, by input. A blank input is
 * left out, so the backend fills in its default; a blank required one is an error. A
 * string or array that has a default starts filled with it, so blanking it is a choice:
 * it is sent empty (`""`, `[]`), never swapped back for the default (#2662).
 */
export function toInputs(
  fields: InputField[],
  values: InputValues,
): { inputs: Record<string, unknown>; errors: Record<string, string> } {
  const inputs: Record<string, unknown> = {}
  const errors: Record<string, string> = {}
  for (const field of fields) {
    const raw = values[field.name]
    if (field.type === 'boolean') {
      inputs[field.name] = raw === true
      continue
    }
    const text = typeof raw === 'string' ? raw : ''
    if (text.trim() === '') {
      if (field.default != null && !field.enum && (field.type === 'string' || field.type === 'array')) {
        // A string as typed: spaces someone entered are theirs, not a cleared field.
        inputs[field.name] = field.type === 'string' ? text : []
      } else if (field.required && field.default === undefined) errors[field.name] = 'Required'
      continue
    }
    if (field.type === 'array') {
      const items: unknown[] = []
      text.split('\n').forEach((line, i) => {
        if (line.trim() === '' || errors[field.name]) return
        const parsed = scalar(field.itemType ?? 'string', field.itemType === 'string' ? line : line.trim())
        if (parsed.error) errors[field.name] = `Line ${i + 1}: ${parsed.error}`
        else items.push(parsed.value)
      })
      if (!errors[field.name]) inputs[field.name] = items
      continue
    }
    const parsed = scalar(field.type, text)
    if (parsed.error) errors[field.name] = capital(parsed.error)
    else inputs[field.name] = parsed.value
  }
  return { inputs, errors }
}

/** The editor's schema text: blank is no inputs; otherwise it must be a JSON object. */
export function parseSchemaText(text: string): { schema?: Record<string, unknown> | null; error?: string } {
  if (text.trim() === '') return { schema: null }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (caught) {
    return { error: `Not JSON: ${caught instanceof Error ? caught.message : String(caught)}` }
  }
  return isObject(parsed) ? { schema: parsed } : { error: 'The inputs schema must be a JSON object' }
}

/** The schema as the editor shows it; blank for none. */
export const schemaText = (schema: unknown): string => (isObject(schema) ? JSON.stringify(schema, null, 2) : '')
