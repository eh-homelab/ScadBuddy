import type { InputField } from './flowInputs'

const KIND_WORD: Record<string, string> = { location: 'location', spool: 'spool' }
const article = (word: string) => (/^[aeiou]/i.test(word) ? 'an' : 'a')

/**
 * Why a resolved code does not fit what the field expects, or null. The server's rule
 * (`tasks/answer_check.py` `_accepts`): the code (upper case) is one of `codes`, or its
 * kind is one of `kinds`, or the field names neither.
 */
export function misfit(found: { kind: string; code: string }, expects: InputField['expects']): string | null {
  const kinds = expects?.kinds ?? []
  const codes = (expects?.codes ?? []).map((code) => code.trim().toUpperCase())
  if (kinds.length === 0 && codes.length === 0) return null
  if (codes.includes(found.code.trim().toUpperCase()) || kinds.includes(found.kind)) return null
  if (kinds.length > 0 && codes.length === 0) {
    const words = kinds.map((kind) => KIND_WORD[kind] ?? kind)
    return `Expected ${words.map((word) => `${article(word)} ${word}`).join(' or ')}`
  }
  const wanted = [...kinds.map((kind) => `${article(KIND_WORD[kind] ?? kind)} ${KIND_WORD[kind] ?? kind}`), ...codes]
  return `Expected ${wanted.join(' or ')}`
}
