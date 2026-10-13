import { describe, expect, it } from 'vitest'
import type { LibraryEntry, LibraryFolderView } from '../api/types'
import { folderLabel, projectLosses, splitExtension } from './libraryTransfer'

// #2670 — what a move warns of, and what a rename keeps.

const folders: LibraryFolderView[] = [
  { id: 1, name: 'Projects', parent_id: null, depth: 0 },
  { id: 2, name: 'Lamp', parent_id: 1, depth: 1, project_id: 7, project_name: 'Desk lamp' },
  { id: 3, name: 'Lamp parts', parent_id: 1, depth: 1, project_id: 7, project_name: 'Desk lamp' },
  { id: 4, name: 'Chair', parent_id: 1, depth: 1, project_id: 8, project_name: 'Chair' },
  // A folder under a project's folder is not the project's: Bambuddy lists only the folder's own files.
  { id: 5, name: 'Old', parent_id: 2, depth: 2 },
]

const file = (id: number, folderId: number | null): LibraryEntry => ({
  id,
  filename: `f${id}.3mf`,
  file_type: '3mf',
  folder_id: folderId,
  printable: true,
  has_thumbnail: false,
  is_external: false,
  print_count: 0,
})

describe('projectLosses', () => {
  it("names each project a file leaves, and none it stays in or never was in", () => {
    const files = [file(1, 2), file(2, 2), file(3, 4), file(4, null)]
    expect(projectLosses(files, folders, 5)).toEqual([
      { projectId: 7, name: 'Desk lamp', files: [files[0], files[1]] },
      { projectId: 8, name: 'Chair', files: [files[2]] },
    ])
    // Another folder linked to the same project keeps the file in it.
    expect(projectLosses([file(1, 2)], folders, 3)).toEqual([])
    expect(projectLosses([file(4, null)], folders, null)).toEqual([])
    expect(projectLosses([file(1, 2)], folders, null).map((loss) => loss.name)).toEqual(['Desk lamp'])
  })
})

describe('folderLabel', () => {
  it('reads a folder by its path from the top', () => {
    expect(folderLabel(folders, 5)).toBe('Projects / Lamp / Old')
    expect(folderLabel(folders, null)).toBe('Top level')
  })
})

describe('splitExtension', () => {
  it("keeps a sliced file's whole extension, and a dot inside a name in its stem", () => {
    expect(splitExtension('lamp.gcode.3mf')).toEqual({ stem: 'lamp', extension: '.gcode.3mf' })
    expect(splitExtension('v1.2_shelf.3mf')).toEqual({ stem: 'v1.2_shelf', extension: '.3mf' })
    expect(splitExtension('.hidden')).toEqual({ stem: '.hidden', extension: '' })
    expect(splitExtension('README')).toEqual({ stem: 'README', extension: '' })
  })
})
