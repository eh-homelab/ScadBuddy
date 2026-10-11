import type { LibraryFolderView } from '../api/types'

/**
 * #2165 — the Library page's URL: `/library/<folder>/<subfolder>` selects that folder,
 * each segment a folder's name, URI-encoded so a name with a `/` stays one segment.
 */
export const LIBRARY_ROOT = '/library'

/** The folder names from the page's pathname, decoded; empty at the top level. */
export function folderSegments(pathname: string): string[] {
  const rest = pathname.startsWith(LIBRARY_ROOT) ? pathname.slice(LIBRARY_ROOT.length) : ''
  return rest
    .split('/')
    .filter((segment) => segment !== '')
    .map((segment) => {
      try {
        return decodeURIComponent(segment)
      } catch {
        return segment
      }
    })
}

/** The folder these names lead to, from the top; `undefined` when there is none. */
export function folderIdOf(folders: LibraryFolderView[], segments: string[]): number | null | undefined {
  let parent: number | null = null
  for (const name of segments) {
    const next = folders.find((folder) => (folder.parent_id ?? null) === parent && folder.name === name)
    if (!next) return undefined
    parent = next.id
  }
  return parent
}

/** The folder and its ancestors, the top first. */
export function ancestry(folders: LibraryFolderView[], id: number | null): LibraryFolderView[] {
  const byId = new Map(folders.map((folder) => [folder.id, folder]))
  const chain: LibraryFolderView[] = []
  let at = id === null ? undefined : byId.get(id)
  while (at && !chain.includes(at)) {
    chain.unshift(at)
    at = at.parent_id == null ? undefined : byId.get(at.parent_id)
  }
  return chain
}

/** The page's path for folder `id` (the top level for `null`). */
export function libraryPath(folders: LibraryFolderView[], id: number | null): string {
  const names = ancestry(folders, id).map((folder) => encodeURIComponent(folder.name))
  return names.length === 0 ? LIBRARY_ROOT : `${LIBRARY_ROOT}/${names.join('/')}`
}
