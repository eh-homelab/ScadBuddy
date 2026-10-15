/** #3634 — a library file's name, parted for its card. */
export interface NameParts {
  /** The name without its extension and the suffixes below. */
  stem: string
  /** `3mf`, `gcode.3mf`, `stl`…, lower case; empty when the name has none. */
  extension: string
  /** ` (ScadBuddy)`: a copy ScadBuddy made, of an output or of a library file. */
  scadbuddy: boolean
  /** ` (2)`: a second file of that name, and so on. */
  copy: number | null
}

const SLICED = '.gcode.3mf'
const SCADBUDDY = /\s*\(ScadBuddy\)$/i
const NUMBERED = /\s*\((\d+)\)$/

export function nameParts(filename: string): NameParts {
  // The extension is found from the end: a dot early in a name (`v1.2_shelf….3mf`) is
  // part of the stem.
  const last = filename.lastIndexOf('.')
  const dot = filename.toLowerCase().endsWith(SLICED) ? filename.length - SLICED.length : last > 0 ? last : -1
  let stem = dot > 0 ? filename.slice(0, dot) : filename
  const extension = dot > 0 ? filename.slice(dot + 1).toLowerCase() : ''
  let scadbuddy = false
  let copy: number | null = null
  // ScadBuddy names its copies `<name> (ScadBuddy).3mf`, and a second one `… (2).3mf`.
  for (;;) {
    const numbered: RegExpExecArray | null = copy === null ? NUMBERED.exec(stem) : null
    if (numbered && numbered.index > 0) {
      copy = Number(numbered[1])
      stem = stem.slice(0, numbered.index)
      continue
    }
    const ours: RegExpExecArray | null = scadbuddy ? null : SCADBUDDY.exec(stem)
    if (ours && ours.index > 0) {
      scadbuddy = true
      stem = stem.slice(0, ours.index)
      continue
    }
    break
  }
  return { stem, extension, scadbuddy, copy }
}

/** How long a name is shown whole: two lines of a card at its narrowest (160 px). */
export const SHORT_NAME = 32
const WORD_BREAK = /[\s_-]/

/**
 * #935, #3634 — a long name cut in the middle, at word breaks where it has them, so both
 * its start and its end show: generated names share a long prefix (`bag-clip-3155628…`),
 * and a name's last word is often what tells two apart. A name up to `max` characters is
 * left whole.
 */
export function shortName(name: string, max = SHORT_NAME): string {
  if (name.length <= max) return name
  // The tail starts at a word: the last break that leaves 6 to 16 characters after it.
  let tailStart = name.length - 10
  for (let at = name.length - 7; at >= name.length - 17 && at > 0; at -= 1) {
    if (WORD_BREAK.test(name[at]!)) {
      tailStart = at + 1
      break
    }
  }
  const tail = name.slice(tailStart)
  let head = name.slice(0, max - tail.length - 1)
  // The head ends at a word too, unless that would cost more than 10 characters.
  for (let at = head.length - 1; at >= head.length - 10 && at > 0; at -= 1) {
    if (WORD_BREAK.test(head[at]!)) {
      head = head.slice(0, at)
      break
    }
  }
  return `${head.replace(/[\s_-]+$/, '')}…${tail}`
}
