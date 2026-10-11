// #1290 — the rules for a model's sibling `.scad` files, mirrored from
// `backend/scadbuddy/api/model_files.py` so a name is refused here before the
// route refuses it.

/** The file every render opens, written only through `PUT /models/{slug}/source`. */
export const MAIN_SOURCE = 'model.scad'
/** `SOURCE_FILE_PATTERN`: a bare `.scad` name, no directory, no leading dot. */
export const SOURCE_FILE_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}\.scad$/
/** `MAX_SOURCE_FILES`: how many `.scad` files a model may hold, its own included. */
export const MAX_SOURCE_FILES = 50

/** What a typed name is saved as: `.scad` is added when it is left off. */
export function sourceFileName(typed: string): string {
  const name = typed.trim()
  return name === '' || name.endsWith('.scad') ? name : `${name}.scad`
}

/** Why `name` cannot be a new file of a model holding `existing`, or null when it can. */
export function newSourceFileProblem(name: string, existing: readonly string[]): string | null {
  if (name === MAIN_SOURCE) return `${MAIN_SOURCE} is the model's own source.`
  if (!SOURCE_FILE_PATTERN.test(name)) {
    return 'Use letters, digits, _, . and -, starting with a letter, digit or _, and at most 96 characters before .scad.'
  }
  if (existing.includes(name)) return `This model already has ${name}.`
  if (existing.length >= MAX_SOURCE_FILES) return `A model holds at most ${MAX_SOURCE_FILES} .scad files.`
  return null
}
