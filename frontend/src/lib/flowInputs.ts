/**
 * #2483 — a flow's typed inputs: its inputs schema (`inputs.json` beside its script, a
 * closed subset of JSON Schema the backend checks, `flows/inputs.py`), as the Start run
 * form draws it and the editor reads it. The backend checks the values again when the
 * run starts and fills in the defaults; this only turns the form's text into JSON.
 *
 * #7364 — a prompt's schema is the same subset, and its strings may have a `format`.
 */

export type InputScalar = 'string' | 'integer' | 'number' | 'boolean'
export type InputType = InputScalar | 'array'
export type InputFormat = 'email' | 'date' | 'date-time'
/** #8062 — a task field's `x-sb-widget`. */
export type InputWidget = 'scan' | 'photo' | 'file' | 'entity' | 'weight' | 'confirm'

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
  format?: InputFormat
  minimum?: number
  maximum?: number
  /** #8062 — the field's `x-sb-widget`; an unknown one is left out and logged. */
  widget?: InputWidget
  /** #8062 — an `entity` field's kind. */
  kind?: string
  /** #8062 — a `scan` field's accepted code kinds and codes. */
  expects?: { kinds?: string[]; codes?: string[] }
}

/** What the form holds: text per input, a checkbox's boolean. */
export type InputValues = Record<string, string | boolean>

const SCALARS = new Set(['string', 'integer', 'number', 'boolean'])
const WIDGETS = new Set<unknown>(['scan', 'photo', 'file', 'entity', 'weight', 'confirm'] satisfies InputWidget[])
const FORMATS = new Set<unknown>(['email', 'date', 'date-time'] satisfies InputFormat[])

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const warned = new Set<string>()

/** A property's widget; an unknown name draws the plain field and is logged once. */
function widgetOf(name: string, prop: Record<string, unknown>): InputWidget | undefined {
  const widget = prop['x-sb-widget']
  if (widget === undefined) return undefined
  if (WIDGETS.has(widget)) return widget as InputWidget
  const note = `${name}: unknown x-sb-widget ${JSON.stringify(widget)}`
  if (!warned.has(note)) {
    warned.add(note)
    console.warn(`${note}; the plain field is drawn.`)
  }
  return undefined
}

const strings = (value: unknown): string[] | undefined =>
  Array.isArray(value) && value.every((item) => typeof item === 'string') ? (value as string[]) : undefined

function expectsOf(value: unknown): InputField['expects'] {
  if (!isObject(value)) return undefined
  const kinds = strings(value['kinds'])
  const codes = strings(value['codes'])
  return { ...(kinds ? { kinds } : {}), ...(codes ? { codes } : {}) }
}

/** The weight a task's context asks for: the first `expect` block, in grams. */
export function weightExpected(context: unknown): { weight_g: number; tolerance_g?: number } | undefined {
  if (!Array.isArray(context)) return undefined
  for (const block of context) {
    const target = isObject(block) ? block['expect'] : undefined
    if (isObject(target) && typeof target['weight_g'] === 'number') {
      return {
        weight_g: target['weight_g'],
        ...(typeof target['tolerance_g'] === 'number' ? { tolerance_g: target['tolerance_g'] } : {}),
      }
    }
  }
  return undefined
}

/** "Expect 640 g ± 96 g". */
export const expectedText = ({ weight_g, tolerance_g }: { weight_g: number; tolerance_g?: number }) =>
  `Expect ${weight_g} g${tolerance_g === undefined ? '' : ` ± ${tolerance_g} g`}`

/** The schema's inputs, in its order; none for a flow without one. */
export function inputFields(schema: unknown): InputField[] {
  if (!isObject(schema) || !isObject(schema['properties'])) return []
  const required = new Set(Array.isArray(schema['required']) ? (schema['required'] as string[]) : [])
  return Object.entries(schema['properties']).flatMap(([name, prop]): InputField[] => {
    if (!isObject(prop)) return []
    const type = prop['type']
    if (typeof type !== 'string' || !(SCALARS.has(type) || type === 'array')) return []
    const items = prop['items']
    const widget = widgetOf(name, prop)
    const expects = expectsOf(prop['expects'])
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
        ...(type === 'string' && FORMATS.has(prop['format']) ? { format: prop['format'] as InputFormat } : {}),
        ...(typeof prop['minimum'] === 'number' ? { minimum: prop['minimum'] } : {}),
        ...(typeof prop['maximum'] === 'number' ? { maximum: prop['maximum'] } : {}),
        ...(widget ? { widget } : {}),
        ...(typeof prop['kind'] === 'string' ? { kind: prop['kind'] } : {}),
        ...(expects ? { expects } : {}),
      },
    ]
  })
}

/** #7364 — the titles of the schema's properties that `inputFields` has no field for. */
export function undrawnFields(schema: unknown): string[] {
  if (!isObject(schema) || !isObject(schema['properties'])) return []
  const drawn = new Set(inputFields(schema).map((field) => field.name))
  return Object.entries(schema['properties']).flatMap(([name, prop]) => {
    if (drawn.has(name)) return []
    return [isObject(prop) && typeof prop['title'] === 'string' && prop['title'] ? prop['title'] : name]
  })
}

/** `at` in the browser's time zone as RFC 3339, its offset apart; null for a year it cannot write. */
function localParts(at: Date): { wall: string; offset: string } | null {
  if (Number.isNaN(at.getTime())) return null
  // Whole minutes: an offset has no seconds, and the wall time moves with it.
  const minutes = -Math.trunc(at.getTimezoneOffset())
  const wall = new Date(at.getTime() + minutes * 60_000).toISOString()
  if (!/^\d{4}-/.test(wall)) return null
  const pad = (n: number) => String(n).padStart(2, '0')
  const size = Math.abs(minutes)
  return {
    wall: wall.slice(0, -1).replace(/\.000$/, ''),
    offset: `${minutes < 0 ? '-' : '+'}${pad(Math.floor(size / 60))}:${pad(size % 60)}`,
  }
}

/** A `date-time` value as a `datetime-local` input takes it, in the browser's time zone. */
function localInput(value: string): string {
  const parts = localParts(new Date(value.toUpperCase()))
  return parts ? parts.wall.replace(/:00$/, '') : ''
}

/** What a `datetime-local` input holds, with the offset the browser's zone has at that time. */
function withOffset(text: string): string | null {
  const parts = localParts(new Date(text))
  return parts && parts.wall + parts.offset
}

const isDateTime = (field: InputField) => field.format === 'date-time' && !field.enum
const isDated = (field: InputField) => isDateTime(field) || (field.format === 'date' && !field.enum)

/** A field's default as a person reads it: a date-time in local time. */
export function defaultText(field: InputField): string {
  const { default: value } = field
  if (value === undefined || value === null) return ''
  if (isDateTime(field)) {
    const at = new Date(String(value).toUpperCase())
    if (!Number.isNaN(at.getTime())) return at.toLocaleString()
  }
  return Array.isArray(value) ? value.join(', ') : String(value)
}

/** An array's items as typed, one per line, with the line of each: a blank line is no item. */
export function itemLines(text: string): { line: number; text: string }[] {
  return text.split('\n').flatMap((item, i) => (item.trim() === '' ? [] : [{ line: i + 1, text: item }]))
}

/** The form at each input's default: an array one item per line. */
export function initialValues(fields: InputField[]): InputValues {
  return Object.fromEntries(
    fields.map((field) => {
      if (field.type === 'boolean') return [field.name, field.default === true]
      if (field.default === undefined || field.default === null) return [field.name, '']
      if (Array.isArray(field.default)) return [field.name, field.default.map(String).join('\n')]
      if (isDateTime(field)) return [field.name, localInput(String(field.default))]
      return [field.name, String(field.default)]
    }),
  )
}

interface Parsed {
  value?: unknown
  error?: string
}

function scalar(type: InputScalar, text: string): Parsed {
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

/** Errors by field: no prototype, because a field may be named `constructor`. */
export const noErrors = (): Record<string, string> => Object.create(null) as Record<string, string>

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)

/**
 * The run's inputs from the form, and what does not parse, by input. A blank input is
 * left out, so the backend fills in its default; a blank required one is an error. A
 * string or array that has a default starts filled with it, so blanking it is a choice:
 * it is sent empty (`""`, `[]`), never swapped back for the default (#2662).
 *
 * #7364 — with `answer`, a prompt's: every blank is left out, and a blank required one is
 * an error whatever its default, because a task's answer is checked before its defaults.
 * `unread` are the fields that only look blank: the browser could not read what they hold.
 */
export function toInputs(
  fields: InputField[],
  values: InputValues,
  { answer = false, unread = [] }: { answer?: boolean; unread?: readonly string[] } = {},
): { inputs: Record<string, unknown>; errors: Record<string, string> } {
  const inputs: Record<string, unknown> = {}
  const errors = noErrors()
  for (const field of fields) {
    const raw = values[field.name]
    if (field.type === 'boolean') {
      inputs[field.name] = raw === true
      continue
    }
    const text = typeof raw === 'string' ? raw : ''
    if (unread.includes(field.name)) {
      const number = field.type === 'integer' || field.type === 'number' ? scalar(field.type, '') : ({} as Parsed)
      errors[field.name] = capital(number.error ?? 'this is not complete')
      continue
    }
    if (text.trim() === '') {
      if (answer) {
        if (field.required) errors[field.name] = 'Required'
      } else if (field.default != null && !field.enum && (field.type === 'string' || field.type === 'array')) {
        // A string as typed: spaces someone entered are theirs, not a cleared field.
        inputs[field.name] = field.type === 'string' ? text : []
      } else if (field.required && field.default === undefined) errors[field.name] = 'Required'
      continue
    }
    if (field.type === 'array') {
      const items: unknown[] = []
      for (const { line, text: item } of itemLines(text)) {
        const parsed = scalar(field.itemType ?? 'string', field.itemType === 'string' ? item : item.trim())
        if (parsed.error) {
          errors[field.name] = `Line ${line}: ${parsed.error}`
          break
        }
        items.push(parsed.value)
      }
      if (!errors[field.name]) inputs[field.name] = items
      continue
    }
    // A date input takes it, and no server does.
    if (isDated(field) && /^\d{5}/.test(text)) {
      errors[field.name] = 'The year must have four digits'
      continue
    }
    if (isDateTime(field)) {
      // Untouched, it is sent as it came: the local time has lost its offset.
      const untouched = typeof field.default === 'string' && localInput(field.default) === text
      const sent = untouched ? field.default : withOffset(text)
      if (sent === null) errors[field.name] = 'A date and a time'
      else inputs[field.name] = sent
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
