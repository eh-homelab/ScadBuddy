import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { SourceDiff } from './SourceDiff'

// Monaco needs layout, workers and a canvas, none of which jsdom has (SourceEditor.test).
vi.mock('../lib/monaco', () => ({ OPENSCAD_LANGUAGE_ID: 'openscad', setupMonaco: vi.fn() }))
vi.mock('@monaco-editor/react', () => ({
  DiffEditor: ({
    original,
    modified,
    language,
    options,
  }: {
    original: string
    modified: string
    language: string
    options: { readOnly?: boolean; originalEditable?: boolean }
  }) => (
    <div
      data-testid="monaco-diff"
      data-original={original}
      data-modified={modified}
      data-language={language}
      data-readonly={String(!!options.readOnly && !options.originalEditable)}
    />
  ),
}))

describe('SourceDiff (#1287)', () => {
  it('shows theirs against the buffer as OpenSCAD, with neither side editable', () => {
    render(<SourceDiff original="cube(3);" modified="sphere(2);" />)
    const diff = screen.getByTestId('monaco-diff')
    expect(diff).toHaveAttribute('data-original', 'cube(3);')
    expect(diff).toHaveAttribute('data-modified', 'sphere(2);')
    expect(diff).toHaveAttribute('data-language', 'openscad')
    expect(diff).toHaveAttribute('data-readonly', 'true')
  })
})
