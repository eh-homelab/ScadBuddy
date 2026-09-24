import { StreamLanguage, type StreamParser } from '@codemirror/language'
import { tags } from '@lezer/highlight'

/**
 * A small OpenSCAD mode for CodeMirror, written here rather than pulled from a
 * C-like legacy mode: OpenSCAD's `module`/`function` declarations, its `$fn`-style
 * special variables and its customizer annotations (`// [1:100]`) are the things
 * worth colouring, and none of them are C.
 */

const KEYWORDS = new Set([
  'module',
  'function',
  'include',
  'use',
  'if',
  'else',
  'for',
  'intersection_for',
  'let',
  'each',
  'return',
  'echo',
  'assert',
])

const ATOMS = new Set(['true', 'false', 'undef', 'PI'])

const BUILTINS = new Set([
  'children',
  'circle',
  'color',
  'cube',
  'cylinder',
  'difference',
  'hull',
  'import',
  'intersection',
  'linear_extrude',
  'minkowski',
  'mirror',
  'multmatrix',
  'offset',
  'polygon',
  'polyhedron',
  'projection',
  'render',
  'resize',
  'rotate',
  'rotate_extrude',
  'scale',
  'sphere',
  'square',
  'surface',
  'text',
  'translate',
  'union',
])

interface ScadState {
  inBlockComment: boolean
}

export const openscad: StreamParser<ScadState> = {
  name: 'openscad',
  startState: () => ({ inBlockComment: false }),

  token(stream, state) {
    if (state.inBlockComment) {
      while (!stream.eol()) {
        if (stream.match('*/')) {
          state.inBlockComment = false
          break
        }
        stream.next()
      }
      return 'comment'
    }
    if (stream.eatSpace()) return null

    if (stream.match('//')) {
      stream.skipToEnd()
      return 'comment'
    }
    if (stream.match('/*')) {
      state.inBlockComment = true
      return 'comment'
    }
    if (stream.match(/^"(?:[^"\\]|\\.)*"?/)) return 'string'
    if (stream.match(/^<[^>\n]*>/)) return 'string'
    if (stream.match(/^\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|^\.\d+/)) return 'number'
    if (stream.match(/^\$[A-Za-z_]\w*/)) return 'special'

    if (stream.match(/^[A-Za-z_]\w*/)) {
      const name = stream.current()
      if (KEYWORDS.has(name)) return 'keyword'
      if (ATOMS.has(name)) return 'atom'
      if (BUILTINS.has(name)) return 'builtin'
      return 'variable'
    }

    if (stream.match(/^[+\-*/%!<>=&|?:]+/)) return 'operator'
    stream.next()
    return null
  },

  tokenTable: {
    special: tags.special(tags.variableName),
    builtin: tags.standard(tags.variableName),
  },

  languageData: {
    commentTokens: { line: '//', block: { open: '/*', close: '*/' } },
    closeBrackets: { brackets: ['(', '[', '{', '"'] },
  },
}

export const openscadLanguage = StreamLanguage.define(openscad)
