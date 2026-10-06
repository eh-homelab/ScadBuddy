import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type * as Monaco from 'monaco-editor/editor/editor.api'

interface Opener {
  openCodeEditor(source: unknown, resource: Monaco.Uri, selection?: object): boolean
}

// Monaco's own URI class (the editor needs a DOM; this does not). The opener gets one of
// these, whose `toString()` percent-encodes: `BOSL2@<commit>` is `BOSL2%40<commit>` (#185).
const { URI } = await vi.hoisted(() =>
  vi.importActual<{ URI: typeof Monaco.Uri }>('monaco-editor/base/common/uri.js'),
)

/** Monaco's own key codes for the keys the editor binds. */
const KEY = vi.hoisted(() => ({ CtrlCmd: 2048, Tab: 2, Shift: 4, Ctrl: 5, Alt: 6, Escape: 9, KeyS: 49, Meta: 57 }))
/** A DOM key as Monaco's `KeyCode`; 0 for the ones nothing here binds. */
const keyCode = (key: string): number =>
  ({ Escape: KEY.Escape, Tab: KEY.Tab, Shift: KEY.Shift, Control: KEY.Ctrl, Alt: KEY.Alt, Meta: KEY.Meta, s: KEY.KeyS })[
    key
  ] ?? 0

const dispose = vi.fn()
/** URIs Monaco has no text model for. */
const absent = new Set<string>()
const getModel = vi.fn((uri: string | Monaco.Uri) =>
  absent.has(URI.parse(String(uri)).toString()) ? null : { dispose, getValue: () => 'cube(1);' },
)
const setModelMarkers = vi.fn()
const openers: Opener[] = []
const registerEditorOpener = vi.fn((opener: Opener) => {
  openers.push(opener)
  return { dispose: () => openers.splice(openers.indexOf(opener), 1) }
})

// Monaco itself needs layout, workers and a canvas, none of which jsdom has; what is
// under test here is the lifecycle this component owns, not the editor.
vi.mock('../lib/monaco', () => ({
  OPENSCAD_LANGUAGE_ID: 'openscad',
  setupMonaco: vi.fn(),
  monaco: {
    editor: { getModel, setModelMarkers, registerEditorOpener },
    Uri: URI,
    KeyMod: { CtrlCmd: KEY.CtrlCmd },
    KeyCode: KEY,
  },
}))

const disconnect = vi.fn()
const connectLanguageServer = vi.fn((..._args: unknown[]) => ({ dispose: disconnect }))
vi.mock('../lib/languageClient', () => ({ connectLanguageServer }))

/** The one editor instance the mocked `Editor` hands over, showing whatever `path` is now. */
const instance = {
  path: '',
  changed: undefined as (() => void) | undefined,
  // One text model per URI, as Monaco keeps them.
  models: new Map<string, { uri: string; getValue: () => string }>(),
  getModel: () => {
    let model = instance.models.get(instance.path)
    if (!model) {
      model = { uri: instance.path, getValue: () => 'cube(1);' }
      instance.models.set(instance.path, model)
    }
    return model
  },
  options: {} as { tabFocusMode?: boolean },
  updateOptions: (options: { tabFocusMode?: boolean }) => Object.assign(instance.options, options),
  keyListeners: [] as ((event: { keyCode: number }) => void)[],
  onKeyDown: (listener: (event: { keyCode: number }) => void) => {
    instance.keyListeners.push(listener)
    return { dispose: () => {} }
  },
  blurListeners: [] as (() => void)[],
  onDidBlurEditorText: (listener: () => void) => {
    instance.blurListeners.push(listener)
    return { dispose: () => {} }
  },
  onDidChangeModel: (listener: () => void) => {
    instance.changed = listener
    return { dispose: () => {} }
  },
  /** `addCommand`'s keybindings, run by the mocked editor's own keydown as Monaco would. */
  commands: new Map<number, () => void>(),
  addCommand: (keybinding: number, handler: () => void) => {
    instance.commands.set(keybinding, handler)
    return null
  },
  setSelection: vi.fn(),
  revealRangeInCenterIfOutsideViewport: vi.fn(),
  setPosition: vi.fn(),
  revealPositionInCenterIfOutsideViewport: vi.fn(),
}

// Mounts the way the real one does: once, handing over an editor holding the path's
// model, and switching that model (with a change event) when `path` changes.
vi.mock('@monaco-editor/react', () => ({
  default: function Editor({
    path,
    value,
    options,
    onMount,
  }: {
    path: string
    value?: string
    options: { readOnly?: boolean }
    onMount: (editor: object) => void
  }) {
    useEffect(() => {
      instance.path = path
      onMount(instance)
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])
    useEffect(() => {
      if (instance.path === path) return
      instance.path = path
      instance.changed?.()
    }, [path])
    return (
      <textarea
        data-testid="monaco"
        data-path={path}
        data-value={value ?? '(held)'}
        data-readonly={String(!!options.readOnly)}
        onBlur={() => instance.blurListeners.forEach((listener) => listener())}
        onKeyDown={(event) => {
          // As Monaco does: its own listeners first, then the keybindings.
          const code = keyCode(event.key.length === 1 ? event.key.toLowerCase() : event.key)
          instance.keyListeners.forEach((listener) => listener({ keyCode: code }))
          // Tab indents, unless tab-focus mode lets the browser move focus.
          if (code === KEY.Tab) {
            if (!instance.options.tabFocusMode) event.preventDefault()
            return
          }
          const command = instance.commands.get(code | (event.ctrlKey || event.metaKey ? KEY.CtrlCmd : 0))
          if (command) {
            event.preventDefault()
            command()
          }
        }}
      />
    )
  },
}))

const { SourceEditor } = await import('./SourceEditor')

const props = { value: 'cube(1);', onChange: () => {}, label: 'OpenSCAD source' }
const MODEL = 'file:///models/a/model.scad'
const LIBRARY = 'file:///libraries/BOSL2@0123456789abcdef0123456789abcdef01234567/shapes3d.scad'
const RANGE = { startLineNumber: 3, startColumn: 8, endLineNumber: 3, endColumn: 14 }

function jump(resource: string, selection: object | undefined = RANGE, source: unknown = instance) {
  const opener = openers.at(-1)
  if (!opener) throw new Error('no editor opener registered')
  let handled = false
  act(() => {
    handled = opener.openCodeEditor(source, URI.parse(resource), selection)
  })
  return handled
}

describe('SourceEditor', () => {
  beforeEach(() => {
    dispose.mockClear()
    getModel.mockClear()
    connectLanguageServer.mockClear()
    disconnect.mockClear()
    absent.clear()
    instance.setSelection.mockClear()
    instance.revealRangeInCenterIfOutsideViewport.mockClear()
    instance.setPosition.mockClear()
    instance.commands.clear()
    instance.keyListeners = []
    instance.blurListeners = []
    instance.options = {}
  })

  describe('keyboard (#997)', () => {
    function renderBetween(extra: Partial<Parameters<typeof SourceEditor>[0]> = {}) {
      return render(
        <>
          <button>Before</button>
          <SourceEditor {...props} uri={MODEL} {...extra} />
          <button>After</button>
        </>,
      )
    }

    it('is not a keyboard trap: Escape, then Tab, leaves the editor', async () => {
      const user = userEvent.setup()
      renderBetween()
      const editor = screen.getByTestId('monaco')

      await user.click(editor)
      await user.tab()
      expect(editor).toHaveFocus()

      await user.keyboard('{Escape}')
      await user.tab()
      expect(screen.getByRole('button', { name: 'After' })).toHaveFocus()
    })

    it('indents again after any other key, and after leaving', async () => {
      const user = userEvent.setup()
      renderBetween()
      const editor = screen.getByTestId('monaco')

      await user.click(editor)
      await user.keyboard('{Escape}a')
      await user.tab()
      expect(editor).toHaveFocus()

      await user.keyboard('{Escape}')
      await user.tab()
      await user.click(editor)
      await user.tab()
      expect(editor).toHaveFocus()
    })

    it('leaves backwards with Escape, then Shift+Tab', async () => {
      const user = userEvent.setup()
      renderBetween()

      await user.click(screen.getByTestId('monaco'))
      await user.keyboard('{Escape}')
      await user.tab({ shift: true })
      expect(screen.getByRole('button', { name: 'Before' })).toHaveFocus()
    })

    it('says how to leave the editor', () => {
      renderBetween()
      expect(screen.getByText('Esc, then Tab, to leave the editor')).toBeVisible()
    })

    it('saves on Ctrl+S and Cmd+S', async () => {
      const user = userEvent.setup()
      const onSave = vi.fn()
      renderBetween({ onSave })

      await user.click(screen.getByTestId('monaco'))
      await user.keyboard('{Control>}s{/Control}')
      expect(onSave).toHaveBeenCalledTimes(1)
      await user.keyboard('{Meta>}s{/Meta}')
      expect(onSave).toHaveBeenCalledTimes(2)
    })

    it('calls the latest save, not the one it mounted with', async () => {
      const user = userEvent.setup()
      const first = vi.fn()
      const second = vi.fn()
      const { rerender } = renderBetween({ onSave: first })
      rerender(
        <>
          <button>Before</button>
          <SourceEditor {...props} uri={MODEL} onSave={second} />
          <button>After</button>
        </>,
      )

      await user.click(screen.getByTestId('monaco'))
      await user.keyboard('{Control>}s{/Control}')
      expect(first).not.toHaveBeenCalled()
      expect(second).toHaveBeenCalledTimes(1)
    })
  })

  it('disposes the text model it opened when it unmounts', () => {
    const { unmount } = render(<SourceEditor {...props} uri={MODEL} />)
    expect(dispose).not.toHaveBeenCalled()

    unmount()
    expect(getModel.mock.calls.map(([uri]) => String(uri))).toContain(MODEL)
    expect(dispose).toHaveBeenCalledTimes(1)
  })

  it('disposes the previous model when the uri changes', () => {
    const { rerender } = render(<SourceEditor {...props} uri={MODEL} />)
    rerender(<SourceEditor {...props} uri="file:///models/b/model.scad" />)

    expect(getModel.mock.calls.map(([uri]) => String(uri))).toContain(MODEL)
    expect(dispose).toHaveBeenCalledTimes(1)
  })

  it('runs a language server session for the model while it is open', () => {
    const readFile = vi.fn()
    const { unmount } = render(
      <SourceEditor {...props} uri={MODEL} languageServer="/api/v1/models/a/lsp" readFile={readFile} />,
    )
    expect(connectLanguageServer).toHaveBeenCalledWith(
      expect.objectContaining({ uri: MODEL }),
      '/api/v1/models/a/lsp',
      readFile,
    )

    unmount()
    expect(disconnect).toHaveBeenCalledTimes(1)
  })

  it('runs none without a server to talk to', () => {
    render(<SourceEditor {...props} uri={MODEL} />)
    expect(connectLanguageServer).not.toHaveBeenCalled()
  })

  describe('go to definition in another file (#185)', () => {
    it('shows the file read-only where the jump pointed, and goes back to the model', () => {
      render(<SourceEditor {...props} uri={MODEL} languageServer="/api/v1/models/a/lsp" />)

      expect(jump(LIBRARY)).toBe(true)
      const editor = screen.getByTestId('monaco')
      expect(editor.dataset.path).toBe(URI.parse(LIBRARY).toString())
      expect(editor.dataset.readonly).toBe('true')
      // The wrapper would write `value` into the library's model.
      expect(editor.dataset.value).toBe('(held)')
      expect(screen.getByTestId('definition-bar')).toHaveTextContent('BOSL2/shapes3d.scad — read-only')
      expect(instance.setSelection).toHaveBeenCalledWith(RANGE)
      expect(instance.revealRangeInCenterIfOutsideViewport).toHaveBeenCalledWith(RANGE)

      fireEvent.click(screen.getByRole('button', { name: /Back to model\.scad/ }))
      expect(editor.dataset.path).toBe(MODEL)
      expect(editor.dataset.readonly).toBe('false')
      expect(editor.dataset.value).toBe('cube(1);')
      expect(screen.queryByTestId('definition-bar')).toBeNull()
    })

    it('keeps the language server on the model while a file is shown', () => {
      render(<SourceEditor {...props} uri={MODEL} languageServer="/api/v1/models/a/lsp" />)
      jump(LIBRARY)
      fireEvent.click(screen.getByRole('button', { name: /Back to/ }))

      expect(connectLanguageServer).toHaveBeenCalledTimes(1)
      expect(disconnect).not.toHaveBeenCalled()
    })

    it('opens a sibling file by its path in the model directory', () => {
      render(<SourceEditor {...props} uri={MODEL} />)
      jump('file:///models/a/parts/helper.scad', { lineNumber: 2, column: 1 })

      expect(screen.getByTestId('definition-bar')).toHaveTextContent('parts/helper.scad')
      expect(instance.setPosition).toHaveBeenCalledWith({ lineNumber: 2, column: 1 })
    })

    it("opens a pinned library's file read-only, though Monaco encodes the @ in its URI", () => {
      render(<SourceEditor {...props} uri={MODEL} />)
      const resource = URI.parse(LIBRARY)
      expect(resource.toString()).toContain('BOSL2%40')

      expect(jump(LIBRARY)).toBe(true)
      expect(screen.getByTestId('definition-bar')).toHaveTextContent('BOSL2/shapes3d.scad — read-only')
      expect(screen.getByTestId('monaco').dataset.readonly).toBe('true')
    })

    it('leaves jumps it cannot show to Monaco', () => {
      render(<SourceEditor {...props} uri={MODEL} />)
      absent.add(URI.parse('file:///libraries/BOSL2@0123456789abcdef0123456789abcdef01234567/unfetched.scad').toString())

      expect(jump('file:///libraries/BOSL2@0123456789abcdef0123456789abcdef01234567/unfetched.scad')).toBe(false)
      expect(jump('file:///usr/share/openscad/libraries/MCAD/units.scad')).toBe(false)
      expect(jump(LIBRARY, RANGE, { another: 'editor' })).toBe(false)
      expect(screen.getByTestId('monaco').dataset.path).toBe(MODEL)
    })

    it('shows the model again when the editor opens another one', () => {
      const { rerender } = render(<SourceEditor {...props} uri={MODEL} />)
      jump(LIBRARY)
      rerender(<SourceEditor {...props} uri="file:///models/b/model.scad" />)

      expect(screen.getByTestId('monaco').dataset.path).toBe('file:///models/b/model.scad')
      expect(screen.queryByTestId('definition-bar')).toBeNull()
    })

    it('moves the cursor for a jump within the model it already shows', () => {
      render(<SourceEditor {...props} uri={MODEL} />)
      instance.setPosition.mockClear()

      expect(jump(MODEL, { lineNumber: 4, column: 2 })).toBe(true)
      expect(instance.setPosition).toHaveBeenCalledWith({ lineNumber: 4, column: 2 })
      expect(instance.revealPositionInCenterIfOutsideViewport).toHaveBeenCalledWith({ lineNumber: 4, column: 2 })
      expect(screen.getByTestId('monaco').dataset.path).toBe(MODEL)
    })

    it('goes back to the model for a jump into it from a shown file, to where it pointed', () => {
      render(<SourceEditor {...props} uri={MODEL} />)
      jump(LIBRARY)
      instance.setSelection.mockClear()

      expect(jump(MODEL, RANGE)).toBe(true)
      expect(screen.getByTestId('monaco').dataset.path).toBe(MODEL)
      expect(screen.queryByTestId('definition-bar')).toBeNull()
      expect(instance.setSelection).toHaveBeenCalledWith(RANGE)
    })

    it('comes back to a model on its own source, not the file it last showed', () => {
      // The file's text model went with the old session: reopening it would be empty.
      const { rerender } = render(<SourceEditor {...props} uri={MODEL} />)
      jump(LIBRARY)
      rerender(<SourceEditor {...props} uri="file:///models/b/model.scad" />)
      instance.setSelection.mockClear()
      rerender(<SourceEditor {...props} uri={MODEL} />)

      const editor = screen.getByTestId('monaco')
      expect(editor.dataset.path).toBe(MODEL)
      expect(editor.dataset.value).toBe('cube(1);')
      expect(editor.dataset.readonly).toBe('false')
      expect(screen.queryByTestId('definition-bar')).toBeNull()
      expect(instance.setSelection).not.toHaveBeenCalled()
    })

    it('unregisters its opener when it unmounts', () => {
      const { unmount } = render(<SourceEditor {...props} uri={MODEL} />)
      const before = openers.length
      unmount()
      expect(openers).toHaveLength(before - 1)
    })
  })
})
