import type { InputField } from './flowInputs'

const KIND_WORD: Record<string, string> = { location: 'location', spool: 'spool' }
const article = (word: string) => (/^[aeiou]/i.test(word) ? 'an' : 'a')

/** Why a resolved code does not fit what the field expects, or null. */
export function misfit(found: { kind: string; code: string }, expects: InputField['expects']): string | null {
  const kinds = expects?.kinds
  if (kinds && kinds.length > 0 && !kinds.includes(found.kind)) {
    const words = kinds.map((kind) => KIND_WORD[kind] ?? kind)
    return `Expected ${words.map((word) => `${article(word)} ${word}`).join(' or ')}`
  }
  const codes = expects?.codes
  if (codes && codes.length > 0 && !codes.includes(found.code)) return `Expected ${codes.join(' or ')}`
  return null
}
