import type { ModelLibrary } from '../api/types'

/** "dotSCAD v3.3 · bb33edf": a library as an output was rendered with it (#1296). */
export function libraryLabel(library: ModelLibrary): string {
  return `${library.name} ${library.ref} · ${library.commit.slice(0, 7)}`
}

/**
 * How the model's pin now differs from what the output was rendered with, or null when
 * it is the same commit, or the model's pins are not known yet.
 */
export function pinChange(library: ModelLibrary, pinned: ModelLibrary[] | undefined): string | null {
  if (pinned === undefined) return null
  const now = pinned.find((pin) => pin.name === library.name)
  if (!now) return 'no longer pinned'
  return now.commit === library.commit ? null : `now ${libraryLabel(now)}`
}
