import { render } from '@testing-library/react'
import { useEffect } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { FLOW_HOST_FUNCTIONS } from '../../mocks/features/flows'

// #2487 — what the flow editor adds to Monaco: the check's problems as markers on the
// script's model, and completion and hover for its model only. Monaco itself needs
// layout and workers jsdom lacks (SourceEditor.test), so it is a stand-in here.

interface Provider {
  provideCompletionItems?: (model: unknown, position: unknown) => { suggestions: { label: string; insertText: string }[] }
  provideHover?: (model: unknown, position: unknown) => { contents: { value: string }[] } | null
}
const providers = vi.hoisted(() => ({ completion: [] as Provider[], hover: [] as Provider[] }))
const setModelMarkers = vi.hoisted(() => vi.fn())

vi.mock('../../lib/monaco', () => ({
  PYTHON_LANGUAGE_ID: 'python',
  setupMonaco: vi.fn(),
  monaco: {
    editor: { setModelMarkers, getModel: () => null },
    Uri: { parse: (uri: string) => uri },
    KeyMod: { CtrlCmd: 2048 },
    KeyCode: { Escape: 9, Tab: 2, Shift: 4, Ctrl: 5, Alt: 6, Meta: 57, KeyS: 49 },
    Range: class {
      constructor(...args: number[]) {
        Object.assign(this, { args })
      }
    },
    languages: {
      CompletionItemKind: { Function: 1, Struct: 2, Value: 3 },
      CompletionItemInsertTextRule: { InsertAsSnippet: 4 },
      registerCompletionItemProvider: (_language: string, provider: Provider) => providers.completion.push(provider),
      registerHoverProvider: (_language: string, provider: Provider) => providers.hover.push(provider),
    },
  },
}))

const SCRIPT = 'async def main():\n    await tool("li\n'

/** A text model at `path` holding SCRIPT. */
function model(path: string) {
  const lines = SCRIPT.split('\n')
  return {
    uri: { path },
    getValue: () => SCRIPT,
    getLineContent: (n: number) => lines[n - 1] ?? '',
    getWordUntilPosition: () => ({ word: 'li', startColumn: 17, endColumn: 19 }),
    getWordAtPosition: () => ({ word: 'tool', startColumn: 11, endColumn: 15 }),
  }
}
const script = model('/flows/def-swap/swap/flow.py')

vi.mock('@monaco-editor/react', () => ({
  default: function Editor({ onMount, path }: { onMount: (editor: unknown) => void; path: string }) {
    useEffect(() => {
      onMount({
        getModel: () => script,
        addCommand: vi.fn(),
        onKeyDown: vi.fn(),
        onDidBlurEditorText: vi.fn(),
        updateOptions: vi.fn(),
      })
    }, [onMount])
    return <div data-path={path} />
  },
}))

const { FlowEditor } = await import('./FlowEditor')

describe('FlowEditor', () => {
  it("marks the check's problems by line, and completes and hovers on a flow's script only", () => {
    const { container, rerender } = render(
      <FlowEditor version="def-swap" name="swap" initialValue={SCRIPT} onChange={vi.fn()} problems={[]} onSave={vi.fn()} label="Script" />,
    )
    expect(container.querySelector('[data-path]')).toHaveAttribute('data-path', 'file:///flows/def-swap/swap/flow.py')
    rerender(
      <FlowEditor
        version="def-swap"
        name="swap"
        initialValue={SCRIPT}
        onChange={vi.fn()}
        problems={[{ line: 2, message: 'error[unresolved-reference]' }]}
        hostFunctions={FLOW_HOST_FUNCTIONS}
        onSave={vi.fn()}
        label="Script"
      />,
    )
    const [, owner, markers] = setModelMarkers.mock.calls.at(-1)!
    expect(owner).toBe('flow-check')
    expect(markers).toEqual([expect.objectContaining({ startLineNumber: 2, message: 'error[unresolved-reference]' })])

    expect(providers.completion).toHaveLength(1)
    const position = { lineNumber: 2, column: 19 }
    expect(providers.completion[0]!.provideCompletionItems!(script, position).suggestions.map((s) => s.label)).toEqual([
      'list_models',
      'send_to_bambuddy',
    ])
    expect(providers.completion[0]!.provideCompletionItems!(model('/models/box/model.scad'), position).suggestions).toEqual(
      [],
    )
    expect(providers.hover[0]!.provideHover!(script, { lineNumber: 2, column: 12 })?.contents[0]?.value).toContain(
      'async def tool(name: str, args: dict[str, Any]) -> str',
    )
  })
})
