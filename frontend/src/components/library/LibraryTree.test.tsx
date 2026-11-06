import { fireEvent, render, screen } from '@testing-library/react'
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

describe('LibraryTree while filtering (#5291 review)', () => {
  const folders: LibraryFolderView[] = [
    { id: 3, name: 'Supplies', parent_id: null, depth: 0 },
    { id: 4, name: 'Storage Boxes', parent_id: 3, depth: 1 },
    { id: 5, name: 'Bins', parent_id: 4, depth: 2 },
  ]

  it('opens every folder left, and its chevron and the arrow keys still fold and open them', () => {
    render(<LibraryTree folders={folders} selected={null} onSelect={vi.fn()} filter="bins" />)
    const supplies = screen.getByRole('treeitem', { name: 'Supplies' })
    expect(supplies).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('treeitem', { name: 'Storage Boxes' })).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(screen.getByTestId('library-folder-toggle-3'))
    expect(supplies).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(screen.getByTestId('library-folder-toggle-3'))
    expect(supplies).toHaveAttribute('aria-expanded', 'true')

    fireEvent.keyDown(supplies, { key: 'ArrowLeft' })
    expect(supplies).toHaveAttribute('aria-expanded', 'false')
    fireEvent.keyDown(supplies, { key: 'ArrowRight' })
    expect(supplies).toHaveAttribute('aria-expanded', 'true')
  })

  it('opens every folder left again when the filter changes', () => {
    const { rerender } = render(<LibraryTree folders={folders} selected={null} onSelect={vi.fn()} filter="bins" />)
    fireEvent.click(screen.getByTestId('library-folder-toggle-3'))
    expect(screen.getByRole('treeitem', { name: 'Supplies' })).toHaveAttribute('aria-expanded', 'false')
    rerender(<LibraryTree folders={folders} selected={null} onSelect={vi.fn()} filter="box" />)
    expect(screen.getByRole('treeitem', { name: 'Supplies' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('opens them all again when the filter goes back to an earlier text (#5306 review)', () => {
    const { rerender } = render(<LibraryTree folders={folders} selected={null} onSelect={vi.fn()} filter="bins" />)
    fireEvent.click(screen.getByTestId('library-folder-toggle-4'))
    expect(screen.getByRole('treeitem', { name: 'Storage Boxes' })).toHaveAttribute('aria-expanded', 'false')
    rerender(<LibraryTree folders={folders} selected={null} onSelect={vi.fn()} filter="bin" />)
    rerender(<LibraryTree folders={folders} selected={null} onSelect={vi.fn()} filter="bins" />)
    expect(screen.getByRole('treeitem', { name: 'Storage Boxes' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('keeps a folder folded when the folders are read again under the same filter (#5303 review)', () => {
    const { rerender } = render(<LibraryTree folders={folders} selected={null} onSelect={vi.fn()} filter="bins" />)
    fireEvent.click(screen.getByTestId('library-folder-toggle-4'))
    expect(screen.getByRole('treeitem', { name: 'Storage Boxes' })).toHaveAttribute('aria-expanded', 'false')
    // A rename, move or create elsewhere on the page reads the tree again: a new array.
    rerender(<LibraryTree folders={folders.map((folder) => ({ ...folder }))} selected={null} onSelect={vi.fn()} filter="bins" />)
    expect(screen.getByRole('treeitem', { name: 'Storage Boxes' })).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('treeitem', { name: 'Supplies' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('forgets a fold once its folder is gone, so it opens when it comes back (#5303 review)', () => {
    const { rerender } = render(<LibraryTree folders={folders} selected={null} onSelect={vi.fn()} filter="bins" />)
    fireEvent.click(screen.getByTestId('library-folder-toggle-4'))
    const without: LibraryFolderView[] = [folders[0]!, { id: 6, name: 'Old bins', parent_id: 3, depth: 1 }]
    rerender(<LibraryTree folders={without} selected={null} onSelect={vi.fn()} filter="bins" />)
    expect(screen.queryByRole('treeitem', { name: 'Storage Boxes' })).not.toBeInTheDocument()
    // Any fold, so the stored state is written while folder 4 is gone.
    fireEvent.click(screen.getByTestId('library-folder-toggle-3'))
    fireEvent.click(screen.getByTestId('library-folder-toggle-3'))
    rerender(<LibraryTree folders={[...folders, without[1]!]} selected={null} onSelect={vi.fn()} filter="bins" />)
    expect(screen.getByRole('treeitem', { name: 'Storage Boxes' })).toHaveAttribute('aria-expanded', 'true')
  })
})
