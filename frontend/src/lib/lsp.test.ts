import { describe, expect, it } from 'vitest'
import {
  directoryOf,
  socketUrl,
  toCompletion,
  toEdits,
  toHover,
  toLocations,
  toRange,
} from './lsp'

const WORD = { startLineNumber: 3, startColumn: 1, endLineNumber: 3, endColumn: 3 }

describe('toRange', () => {
  it('turns zero-based LSP positions into one-based Monaco ones', () => {
    expect(toRange({ start: { line: 0, character: 0 }, end: { line: 2, character: 4 } })).toEqual({
      startLineNumber: 1,
      startColumn: 1,
      endLineNumber: 3,
      endColumn: 5,
    })
  })
})

describe('toCompletion', () => {
  it('inserts plain text over the word being typed', () => {
    expect(
      toCompletion(
        {
          label: 'width',
          kind: 6,
          insertText: 'width',
          insertTextFormat: 1,
          documentation: { kind: 'markdown', value: '```scad\nwidth\n```' },
        },
        WORD,
      ),
    ).toEqual({
      label: 'width',
      kind: 4,
      insertText: 'width',
      insertTextRules: 0,
      range: WORD,
      documentation: { value: '```scad\nwidth\n```' },
      detail: undefined,
      filterText: undefined,
      sortText: undefined,
    })
  })

  it('inserts a snippet as a snippet, over the range the server names', () => {
    const item = toCompletion(
      {
        label: 'cube(size, center)',
        kind: 3,
        insertTextFormat: 2,
        textEdit: {
          newText: 'cube(${1:size})',
          range: { start: { line: 2, character: 0 }, end: { line: 2, character: 2 } },
        },
        filterText: 'cube',
      },
      WORD,
    )
    expect(item).toMatchObject({
      kind: 1,
      insertText: 'cube(${1:size})',
      insertTextRules: 4,
      range: WORD,
      filterText: 'cube',
    })
  })

  it('falls back to the label, and to Text for a kind it does not know', () => {
    expect(toCompletion({ label: 'sphere', kind: 99 }, WORD)).toMatchObject({
      insertText: 'sphere',
      kind: 18,
    })
  })

  it('keeps plain-string documentation as it is', () => {
    expect(toCompletion({ label: 'x', documentation: 'plain' }, WORD).documentation).toBe('plain')
  })
})

describe('toHover', () => {
  it('reads markup content', () => {
    expect(
      toHover({
        contents: { kind: 'markdown', value: 'module plate(w=10)' },
        range: { start: { line: 1, character: 0 }, end: { line: 1, character: 5 } },
      }),
    ).toEqual({
      contents: [{ value: 'module plate(w=10)' }],
      range: { startLineNumber: 2, startColumn: 1, endLineNumber: 2, endColumn: 6 },
    })
  })

  it('reads the older marked-string forms', () => {
    expect(toHover({ contents: ['a', { language: 'scad', value: 'cube()' }] })?.contents).toEqual([
      { value: 'a' },
      { value: '```scad\ncube()\n```' },
    ])
  })

  it('is nothing when the server has nothing to say', () => {
    expect(toHover(null)).toBeUndefined()
  })
})

describe('toLocations', () => {
  const range = { start: { line: 1, character: 0 }, end: { line: 1, character: 48 } }
  const monacoRange = { startLineNumber: 2, startColumn: 1, endLineNumber: 2, endColumn: 49 }

  it('accepts a single location, a list, or location links', () => {
    expect(toLocations({ uri: 'file:///a.scad', range })).toEqual([
      { uri: 'file:///a.scad', range: monacoRange },
    ])
    expect(toLocations([{ uri: 'file:///a.scad', range }])).toEqual([
      { uri: 'file:///a.scad', range: monacoRange },
    ])
    expect(
      toLocations([{ targetUri: 'file:///a.scad', targetRange: range, targetSelectionRange: range }]),
    ).toEqual([{ uri: 'file:///a.scad', range: monacoRange }])
    expect(toLocations(null)).toEqual([])
  })
})

describe('toEdits', () => {
  it('maps each edit and treats no answer as no edits', () => {
    expect(
      toEdits([{ newText: '  ', range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } } }]),
    ).toEqual([
      { text: '  ', range: { startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 2 } },
    ])
    expect(toEdits(null)).toEqual([])
  })
})

describe('directoryOf', () => {
  it('is the URI up to and including its last slash', () => {
    expect(directoryOf('file:///models/name-keychain/model.scad')).toBe('file:///models/name-keychain/')
  })
})

describe('socketUrl', () => {
  it('follows the page onto ws or wss, on its own host', () => {
    expect(socketUrl('/api/v1/lsp', 'http://127.0.0.1:8080/new')).toBe('ws://127.0.0.1:8080/api/v1/lsp')
    expect(socketUrl('/api/v1/models/a/lsp', 'https://scadbuddy.example/m/a/source')).toBe(
      'wss://scadbuddy.example/api/v1/models/a/lsp',
    )
  })
})
