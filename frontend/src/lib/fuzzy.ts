/**
 * #3651 — a forgiving match for pickers: every word typed must match some word of the
 * text, as a prefix or substring, as letters in order ("gfb" finds "Gridfinity Bins"),
 * or with one typo ("gridfinty"). Returns 0 for no match, higher for a closer one; cmdk
 * sorts by it.
 */
export function fuzzyScore(text: string, search: string): number {
  const query = words(search)
  if (query.length === 0) return 1
  const haystack = text.toLowerCase()
  const targets = words(text)
  let total = 0
  for (const term of query) {
    const score = termScore(term, haystack, targets)
    if (score === 0) return 0
    total += score
  }
  return total / query.length
}

function words(text: string): string[] {
  return text
    .toLowerCase()
    .split(/[^\p{L}\p{N}]+/u)
    .filter((word) => word !== '')
}

function termScore(term: string, haystack: string, targets: string[]): number {
  if (targets.some((word) => word.startsWith(term))) return 1
  if (haystack.includes(term)) return 0.8
  if (inOrder(term, haystack)) return 0.5
  if (term.length >= 4 && targets.some((word) => withinOneEdit(term, word.slice(0, term.length + 1)))) {
    return 0.3
  }
  return 0
}

function inOrder(term: string, text: string): boolean {
  let at = 0
  for (const char of term) {
    at = text.indexOf(char, at)
    if (at === -1) return false
    at += 1
  }
  return true
}

/** One insertion, deletion, substitution or swap of neighbours, also as a prefix of `word`. */
function withinOneEdit(term: string, word: string): boolean {
  for (const candidate of [word, word.slice(0, term.length), word.slice(0, term.length - 1)]) {
    if (distance(term, candidate) <= 1) return true
  }
  return false
}

function distance(a: string, b: string): number {
  if (Math.abs(a.length - b.length) > 1) return 2
  const rows = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array<number>(b.length).fill(0)])
  for (let j = 1; j <= b.length; j += 1) rows[0]![j] = j
  for (let i = 1; i <= a.length; i += 1) {
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      let best = Math.min(rows[i - 1]![j]! + 1, rows[i]![j - 1]! + 1, rows[i - 1]![j - 1]! + cost)
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        best = Math.min(best, rows[i - 2]![j - 2]! + 1)
      }
      rows[i]![j] = best
    }
  }
  return rows[a.length]![b.length]!
}
