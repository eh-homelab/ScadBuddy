/**
 * What Python's `str.split()` splits on (`str.isspace()`, Python 3.12), which the server's
 * `_clean_name` (`library/presets.py`) uses. Not JS's `\s`: that also takes U+FEFF, and
 * lacks U+001C-U+001F and U+0085 (#6456).
 */
const PY_WHITESPACE =
  // eslint-disable-next-line no-control-regex -- U+001C-U+001F are whitespace to Python
  /[\t\n\v\f\r\u001c-\u001f \u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/

/** A name as the server stores it: inner runs of whitespace collapsed, ends trimmed (#360). */
export function normalisePresetName(name: string): string {
  return name.split(PY_WHITESPACE).filter(Boolean).join(' ')
}
