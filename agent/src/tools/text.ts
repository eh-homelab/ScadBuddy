/** A string's length as the backend counts it: in code points, as Python's `len()` and
 * so Pydantic's `max_length` and the render's check of a parameter's `max_length`
 * do, where a JS string's `length` (and so Zod's `.max`) counts UTF-16 units and an
 * astral character twice. */
export function codePoints(text: string): number {
  let count = 0
  for (const _ of text) count++
  return count
}
