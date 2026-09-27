import { render } from '@testing-library/react'
import { useEffect } from 'react'
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

const disconnect = vi.fn()
const connectLanguageServer = vi.fn(() => ({ dispose: disconnect }))
vi.mock('../lib/languageClient', () => ({ connectLanguageServer }))

// Mounts the way the real one does: once, handing over an editor holding the path's model.
vi.mock('@monaco-editor/react', () => ({
  default: function Editor({
    path,
    onMount,
  }: {
    path: string
    onMount: (instance: object) => void
  }) {
    useEffect(() => {
      onMount({
        getModel: () => ({ uri: path, getValue: () => 'cube(1);' }),
        updateOptions: () => {},
        onDidChangeModel: () => ({ dispose: () => {} }),
      })
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])
    return <div data-testid="monaco" data-path={path} />
  },
}))

const { SourceEditor } = await import('./SourceEditor')

const props = { value: 'cube(1);', onChange: () => {}, label: 'OpenSCAD source' }

describe('SourceEditor', () => {
  beforeEach(() => {
    dispose.mockClear()
    getModel.mockClear()
    connectLanguageServer.mockClear()
    disconnect.mockClear()
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

  it('runs a language server session for the model while it is open', () => {
    const { unmount } = render(
      <SourceEditor {...props} uri="file:///models/a/model.scad" languageServer="/api/v1/models/a/lsp" />,
    )
    expect(connectLanguageServer).toHaveBeenCalledWith(
      expect.objectContaining({ uri: 'file:///models/a/model.scad' }),
      '/api/v1/models/a/lsp',
    )

    unmount()
    expect(disconnect).toHaveBeenCalledTimes(1)
  })

  it('runs none without a server to talk to', () => {
    render(<SourceEditor {...props} uri="file:///models/a/model.scad" />)
    expect(connectLanguageServer).not.toHaveBeenCalled()
  })
})
