import type { LibraryEntry, LibraryFolderView } from '../api/types'

/** #313 — the mocked Bambuddy library, shaped like tests/bambuddy/recordings. */
export const libraryFolders: LibraryFolderView[] = [
  { id: 1, name: 'MakerWorld', parent_id: null, depth: 0, file_count: 1 },
  { id: 3, name: 'Supplies', parent_id: null, depth: 0, file_count: 0 },
  { id: 4, name: 'Storage', parent_id: 3, depth: 1, file_count: 1 },
  { id: 9, name: 'Bulk', parent_id: null, depth: 0, file_count: 300 },
]

function entry(id: number, filename: string, fileType: string, folderId: number | null): LibraryEntry {
  return {
    id,
    filename,
    file_type: fileType,
    folder_id: folderId,
    has_thumbnail: fileType !== 'stl',
    print_count: 0,
    printable: fileType === '3mf' || fileType === 'stl',
  }
}

export const libraryFiles: LibraryEntry[] = [
  entry(89, 'bag-clip.3mf', '3mf', null),
  entry(104, 'bag-clip.gcode.3mf', 'gcode.3mf', null),
  entry(67, "Clara's Wand.3mf", '3mf', 1),
  entry(46, 'Desiccant_Box.stl', 'stl', 4),
  ...Array.from({ length: 300 }, (_, n) => entry(2000 + n, `part-${n}.3mf`, '3mf', 9)),
]

/** The two-plate file; every other 3MF is one plate and an STL none. */
export const MULTI_PLATE_FILE = 67
