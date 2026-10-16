import type { LibraryEntry, LibraryFolderView } from '../api/types'
import { ancestry } from './libraryPath'

// #2670 — what a library move warns of, and what a rename keeps.

/** A folder's path for a sentence: its names from the top, or "Top level". */
export function folderLabel(folders: LibraryFolderView[], id: number | null): string {
  const names = ancestry(folders, id).map((folder) => folder.name)
  return names.length === 0 ? 'Top level' : names.join(' / ')
}

export interface ProjectLoss {
  projectId: number
  name: string
  files: LibraryEntry[]
}

/**
 * #2670 — the projects a move into `target` takes files out of. Bambuddy lists a project's
 * files as those directly in a folder linked to it, so a file leaves its project when it
 * leaves that folder for any folder not linked to the same project.
 */
export function projectLosses(files: LibraryEntry[], folders: LibraryFolderView[], target: number | null): ProjectLoss[] {
  const byId = new Map(folders.map((folder) => [folder.id, folder]))
  const to = target === null ? undefined : byId.get(target)?.project_id
  const losses = new Map<number, ProjectLoss>()
  for (const file of files) {
    const from = file.folder_id == null ? undefined : byId.get(file.folder_id)
    if (from?.project_id == null || from.project_id === to) continue
    const loss = losses.get(from.project_id) ?? {
      projectId: from.project_id,
      name: from.project_name ?? `project ${from.project_id}`,
      files: [],
    }
    loss.files.push(file)
    losses.set(from.project_id, loss)
  }
  return [...losses.values()]
}

/**
 * A name split into the part a rename edits and the extension it keeps: `.gcode.3mf` for a
 * sliced file, else the last suffix. A leading dot is part of the stem.
 */
export function splitExtension(name: string): { stem: string; extension: string } {
  const sliced = '.gcode.3mf'
  if (name.toLowerCase().endsWith(sliced) && name.length > sliced.length) {
    return { stem: name.slice(0, -sliced.length), extension: name.slice(-sliced.length) }
  }
  const dot = name.lastIndexOf('.')
  return dot > 0 ? { stem: name.slice(0, dot), extension: name.slice(dot) } : { stem: name, extension: '' }
}

/** #3650 — the folders under `id`, and `id` itself: where a folder cannot move to. */
export function folderAndDescendants(folders: LibraryFolderView[], id: number): Set<number> {
  const found = new Set([id])
  let grew = true
  while (grew) {
    grew = false
    for (const folder of folders) {
      if (folder.parent_id != null && found.has(folder.parent_id) && !found.has(folder.id)) {
        found.add(folder.id)
        grew = true
      }
    }
  }
  return found
}
