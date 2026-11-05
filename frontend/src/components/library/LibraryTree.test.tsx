import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { LibraryFolderView } from '../../api/types'
import { filterFolders, LibraryTree } from './LibraryTree'

describe('LibraryTree folder names (#2879)', () => {
  it('names a folder by its visible name alone, its count apart, even one ending in a number', () => {
    const folders: LibraryFolderView[] = [
      { id: 1, name: 'v2', parent_id: null, depth: 0, file_count: 3 },
      { id: 2, name: 'Empty', parent_id: null, depth: 0, file_count: 0 },
      { id: 3, name: 'Single', parent_id: null, depth: 0, file_count: 1 },
    ]
    render(<LibraryTree folders={folders} selected={null} onSelect={vi.fn()} />)

    // "v2", not "v2 3 files": the name ends where the folder's name does.
    const v2 = screen.getByRole('treeitem', { name: 'v2' })
    expect(v2).toHaveAccessibleDescription('3 files')
    expect(screen.getByRole('treeitem', { name: 'Single' })).toHaveAccessibleDescription('1 file')
    expect(screen.getByRole('treeitem', { name: 'Empty' })).not.toHaveAttribute('aria-describedby')
  })
})

describe('filterFolders (#2290)', () => {
  const folders: LibraryFolderView[] = [
    { id: 1, name: 'MakerWorld', parent_id: null, depth: 0 },
    { id: 3, name: 'Supplies', parent_id: null, depth: 0 },
    { id: 4, name: 'Storage Boxes', parent_id: 3, depth: 1 },
    { id: 5, name: 'Bins', parent_id: 4, depth: 2 },
  ]

  it('keeps each folder named so with the folders above it, in order', () => {
    expect(filterFolders(folders, 'BOX').map((folder) => folder.id)).toEqual([3, 4])
    expect(filterFolders(folders, 'bins').map((folder) => folder.id)).toEqual([3, 4, 5])
    expect(filterFolders(folders, 'storage bin')).toEqual([])
  })

  it('keeps every folder when the filter has no words', () => {
    expect(filterFolders(folders, '  ')).toBe(folders)
  })
})
