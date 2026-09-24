import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const dispose = vi.fn()
const getModel = vi.fn(() => ({ dispose, getValue: () => 'cube(1);' }))
const setModelMarkers = vi.fn()

// Monaco itself needs layout, workers and a canvas, none of which jsdom has; what is
// under test here is the lifecycle this component owns, not the editor.
vi.mock('../lib/monaco', () => ({
  OPENSCAD_LANGUAGE_ID: 'openscad',
  setupMonaco: vi.fn(),
  monaco: { editor: { getModel, setModelMarkers }, Uri: { parse: (value: string) => value } },
}))

vi.mock('@monaco-editor/react', () => ({
  default: ({ path }: { path: string }) => <div data-testid="monaco" data-path={path} />,
}))

const { SourceEditor } = await import('./SourceEditor')

const props = { value: 'cube(1);', onChange: () => {}, label: 'OpenSCAD source' }

describe('SourceEditor', () => {
  beforeEach(() => {
    dispose.mockClear()
    getModel.mockClear()
  })

  it('disposes the text model it opened when it unmounts', () => {
    const { unmount } = render(<SourceEditor {...props} uri="file:///models/a/model.scad" />)
    expect(dispose).not.toHaveBeenCalled()

    unmount()
    expect(getModel).toHaveBeenCalledWith('file:///models/a/model.scad')
    expect(dispose).toHaveBeenCalledTimes(1)
  })

  it('disposes the previous model when the uri changes', () => {
    const { rerender } = render(<SourceEditor {...props} uri="file:///models/a/model.scad" />)
    rerender(<SourceEditor {...props} uri="file:///models/b/model.scad" />)

    expect(getModel).toHaveBeenCalledWith('file:///models/a/model.scad')
    expect(dispose).toHaveBeenCalledTimes(1)
  })
})
