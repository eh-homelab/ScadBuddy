/**
 * A tool's input schema as a parameter table (#2171). The schema is untrusted server text:
 * anything that is not an object schema answers no parameters, and every value is read as
 * data, never rendered as markup.
 */

export type Param = {
  name: string
  type: string
  required: boolean
  description: string | null
  enum: string[] | null
  default: string | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function typeWord(type: unknown): string | null {
  if (typeof type === 'string') return type
  if (Array.isArray(type) && type.length > 0 && type.every((t) => typeof t === 'string')) return type.join(' | ')
  return null
}

function typeOf(property: Record<string, unknown>): string {
  const own = typeWord(property.type)
  if (own !== null) return own
  if (Array.isArray(property.enum)) return 'enum'
  const alternatives = Array.isArray(property.anyOf) ? property.anyOf : Array.isArray(property.oneOf) ? property.oneOf : null
  if (alternatives !== null) {
    const words = alternatives.map((a) => (isRecord(a) ? typeWord(a.type) : null)).filter((w): w is string => w !== null)
    if (words.length > 0) return words.join(' | ')
  }
  return 'any'
}

export function parametersOf(schema: unknown): Param[] {
  if (!isRecord(schema) || !isRecord(schema.properties)) return []
  if (schema.type !== undefined && schema.type !== 'object') return []
  const required = new Set(Array.isArray(schema.required) ? schema.required.filter((r) => typeof r === 'string') : [])
  return Object.entries(schema.properties).map(([name, value]) => {
    const property = isRecord(value) ? value : {}
    return {
      name,
      type: typeOf(property),
      required: required.has(name),
      description: typeof property.description === 'string' ? property.description : null,
      enum: Array.isArray(property.enum) ? property.enum.map((e) => (typeof e === 'string' ? e : JSON.stringify(e))) : null,
      default: Object.hasOwn(property, 'default') ? (JSON.stringify(property.default) ?? null) : null,
    }
  })
}
