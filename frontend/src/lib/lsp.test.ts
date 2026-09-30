import { CompletionItemKind } from 'monaco-editor/editor/common/standalone/standaloneEnums.js'
import { describe, expect, it } from 'vitest'
import {
  definitionFile,
  definitionLabel,
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

describe('the completion kind table', () => {
  // LSP's `CompletionItemKind`, in its own order (1-based). Monaco's enum names the same
  // kinds in a different order, and its values are what the editor reads.
  const LSP_KINDS = [
    'Text', 'Method', 'Function', 'Constructor', 'Field', 'Variable', 'Class', 'Interface',
    'Module', 'Property', 'Unit', 'Value', 'Enum', 'Keyword', 'Snippet', 'Color', 'File',
    'Reference', 'Folder', 'EnumMember', 'Constant', 'Struct', 'Event', 'Operator',
    'TypeParameter',
  ] as const

  it.each(LSP_KINDS.map((name, index) => [index + 1, name] as const))(
    'maps LSP kind %i (%s) to the installed Monaco enum',
    (kind, name) => {
      expect(toCompletion({ label: 'x', kind }, WORD).kind).toBe(CompletionItemKind[name])
    },
  )
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

describe('definitionFile', () => {
  const ROOT = 'file:///models/name-keychain/'
  const COMMIT = '0123456789abcdef0123456789abcdef01234567'

  it('names a file beside the model by its path under the model directory', () => {
    expect(definitionFile(`${ROOT}parts/helper.scad`, ROOT)).toEqual({ path: 'parts/helper.scad' })
    expect(definitionFile(`${ROOT}my%20part.scad`, ROOT)).toEqual({ path: 'my part.scad' })
  })

  it('names a library file by the library, the pinned commit and its path in it', () => {
    expect(definitionFile(`file:///libraries/BOSL2@${COMMIT}/shapes3d.scad`, ROOT)).toEqual({
      library: 'BOSL2',
      commit: COMMIT,
      path: 'shapes3d.scad',
    })
    expect(definitionFile(`file:///libraries/NopSCADlib@${COMMIT}/vitamins/screw.scad`, ROOT)).toEqual({
      library: 'NopSCADlib',
      commit: COMMIT,
      path: 'vitamins/screw.scad',
    })
  })

  it('is null anywhere else, and for a URI with no file in it', () => {
    expect(definitionFile('file:///usr/share/openscad/libraries/MCAD/units.scad', ROOT)).toBeNull()
    expect(definitionFile('file:///models/other/helper.scad', ROOT)).toBeNull()
    expect(definitionFile(`file:///libraries/BOSL2@${COMMIT}`, ROOT)).toBeNull()
    expect(definitionFile(`file:///libraries/@${COMMIT}/std.scad`, ROOT)).toBeNull()
    // No commit, or not one: a library URI always names the checkout it is from.
    expect(definitionFile('file:///libraries/BOSL2/shapes3d.scad', ROOT)).toBeNull()
    expect(definitionFile('file:///libraries/BOSL2@HEAD/shapes3d.scad', ROOT)).toBeNull()
    expect(definitionFile(`${ROOT}parts//helper.scad`, ROOT)).toBeNull()
    expect(definitionFile(`${ROOT}bad%E0.scad`, ROOT)).toBeNull()
  })

  it.each([
    '../secret.scad',
    'parts/../../secret.scad',
    './model.scad',
    '%2E%2E/secret.scad',
    'parts/%2e/helper.scad',
    '.git/config',
    'parts/.hidden.scad',
    'parts%2F..%2F..%2Fsecret.scad',
    'parts%5Chelper.scad',
    'parts\\helper.scad',
    'bad%00.scad',
  ])('refuses a path that is not plain, as the backend does: %s', (path) => {
    expect(definitionFile(`${ROOT}${path}`, ROOT)).toBeNull()
    expect(definitionFile(`file:///libraries/BOSL2@${COMMIT}/${path}`, ROOT)).toBeNull()
  })

  it('labels a file the way the editor names it', () => {
    expect(definitionLabel({ path: 'helper.scad' })).toBe('helper.scad')
    expect(definitionLabel({ library: 'BOSL2', path: 'shapes3d.scad' })).toBe('BOSL2/shapes3d.scad')
  })
})
