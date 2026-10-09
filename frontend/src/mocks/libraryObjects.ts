import type { LibraryFileObject } from '../api/types'

/**
 * #1863 — what Arrange reads from a mocked library file ScadBuddy did not make, by file
 * id (`features/library.ts` lists the files): the sliced bag clip is refused, Clara's
 * Wand is two objects, the STL one white one, and any other 3MF one green object.
 */
export const SLICED_LIBRARY_FILE = 104

/** Why Arrange cannot read the sliced file, as the API's detail says it. */
export const SLICED_REASON = 'it is sliced already, so its objects cannot be laid out again'

export function mockLibraryObjects(fileId: number): LibraryFileObject[] | null {
  if (fileId === SLICED_LIBRARY_FILE) return null
  if (fileId === 67) {
    return [
      { part: 'lib1-67-0', name: 'Wand', count: 1, colours: ['#7B1FA2', '#FFD54F'], size: [180, 20, 12], notes: [] },
      { part: 'lib1-67-1', name: 'Star', count: 2, colours: ['#FFD54F'], size: [30, 30, 4], notes: [] },
    ]
  }
  if (fileId === 46) {
    return [
      { part: 'lib1-46-0', name: 'Desiccant_Box', count: 1, colours: ['#FFFFFF'], size: [60, 40, 30], notes: [] },
    ]
  }
  return [{ part: `lib1-${fileId}-0`, name: 'Object', count: 1, colours: ['#43A047'], size: [20, 20, 10], notes: [] }]
}
