import type { InputField } from './flowInputs'

const KIND_WORD: Record<string, string> = { location: 'location', spool: 'spool' }
const article = (word: string) => (/^[aeiou]/i.test(word) ? 'an' : 'a')

/** A code as the server writes it (`codes/parse.py` `parse_code`, then `format_code`): `s03` is `S-3`. Text that is no code stays as it is, upper case. */
export function canonicalCode(text: string): string {
  const upper = text.trim().toUpperCase()
  const found = /^([A-Z])-?([0-9]{1,10})$/.exec(upper)
  return found ? `${found[1]}-${Number(found[2])}` : upper
}

/**
 * Why a resolved code does not fit what the field expects, or null. The server's rule
 * (`tasks/answer_check.py` `_accepts`): the code (as `canonicalCode` writes it) is one of `codes`, or its
 * kind is one of `kinds`, or the field names neither.
 */
export function misfit(found: { kind: string; code: string }, expects: InputField['expects']): string | null {
  const kinds = expects?.kinds ?? []
  const codes = (expects?.codes ?? []).map(canonicalCode)
  if (kinds.length === 0 && codes.length === 0) return null
  if (codes.includes(canonicalCode(found.code)) || kinds.includes(found.kind)) return null
  if (kinds.length > 0 && codes.length === 0) {
    const words = kinds.map((kind) => KIND_WORD[kind] ?? kind)
    return `Expected ${words.map((word) => `${article(word)} ${word}`).join(' or ')}`
  }
  const wanted = [...kinds.map((kind) => `${article(KIND_WORD[kind] ?? kind)} ${KIND_WORD[kind] ?? kind}`), ...codes]
  return `Expected ${wanted.join(' or ')}`
}
