import { render, screen } from '@testing-library/react'
import type * as React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SourceDiff } from './SourceDiff'

const monacoLog = vi.hoisted(() => ({ errors: [] as string[], disposed: [] as string[] }))

// Monaco needs layout, workers and a canvas, none of which jsdom has (SourceEditor.test).
vi.mock('../lib/monaco', () => ({ OPENSCAD_LANGUAGE_ID: 'openscad', setupMonaco: vi.fn() }))
// The diff editor as @monaco-editor/react 4.7 drives it: onMount with the editor, and on
// unmount each model not kept is disposed, then the editor. Monaco's DiffEditorWidget
// reports a model disposed while it still holds it (#2194).
vi.mock('@monaco-editor/react', async () => {
  const { useEffect } = await vi.importActual<typeof React>('react')
  return {
    DiffEditor: ({
      original,
      modified,
      language,
      options,
      keepCurrentOriginalModel = false,
      keepCurrentModifiedModel = false,
      onMount,
    }: {
      original: string
      modified: string
      language: string
      options: { readOnly?: boolean; originalEditable?: boolean }
      keepCurrentOriginalModel?: boolean
      keepCurrentModifiedModel?: boolean
      onMount?: (editor: unknown) => void
    }) => {
      useEffect(() => {
        let editorDisposed = false
        const model = (side: string) => ({
          dispose: () => {
            if (!editorDisposed) monacoLog.errors.push('TextModel got disposed before DiffEditorWidget model got reset')
            monacoLog.disposed.push(side)
          },
        })
        const models = { original: model('original'), modified: model('modified') }
        const editor = { getModel: () => models, dispose: () => (editorDisposed = true) }
        onMount?.(editor)
        return () => {
          if (!keepCurrentOriginalModel) models.original.dispose()
          if (!keepCurrentModifiedModel) models.modified.dispose()
          editor.dispose()
        }
        // Mounted once, as the library mounts its editor once.
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [])
      return (
        <div
          data-testid="monaco-diff"
          data-original={original}
          data-modified={modified}
          data-language={language}
          data-readonly={String(!!options.readOnly && !options.originalEditable)}
        />
      )
    },
  }
})

describe('SourceDiff (#1287)', () => {
  beforeEach(async () => {
    // An earlier test's unmount disposes its models a tick later.
    await new Promise((resolve) => setTimeout(resolve, 0))
    monacoLog.errors = []
    monacoLog.disposed = []
  })

  it('shows theirs against the buffer as OpenSCAD, with neither side editable', () => {
    render(<SourceDiff original="cube(3);" modified="sphere(2);" />)
    const diff = screen.getByTestId('monaco-diff')
    expect(diff).toHaveAttribute('data-original', 'cube(3);')
    expect(diff).toHaveAttribute('data-modified', 'sphere(2);')
    expect(diff).toHaveAttribute('data-language', 'openscad')
    expect(diff).toHaveAttribute('data-readonly', 'true')
  })

  it('disposes both models only after the editor that holds them (#2194)', async () => {
    const { unmount } = render(<SourceDiff original="cube(3);" modified="sphere(2);" />)
    unmount()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(monacoLog.errors).toEqual([])
    expect(monacoLog.disposed.sort()).toEqual(['modified', 'original'])
  })
})
