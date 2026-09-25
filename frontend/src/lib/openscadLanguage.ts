import type * as Monaco from 'monaco-editor/editor/editor.api'

/**
 * An `openscad` language for Monaco: a Monarch tokenizer plus the bracket, comment
 * and auto-closing configuration. Deliberately syntax only — nothing here knows what
 * a module means. Semantic help is a language server's job (#95).
 */

const KEYWORDS = [
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
  'true',
  'false',
  'undef',
]

const BUILTINS = [
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
]

export const openscadConfiguration: Monaco.languages.LanguageConfiguration = {
  comments: { lineComment: '//', blockComment: ['/*', '*/'] },
  brackets: [
    ['{', '}'],
    ['[', ']'],
    ['(', ')'],
  ],
  autoClosingPairs: [
    { open: '{', close: '}' },
    { open: '[', close: ']' },
    { open: '(', close: ')' },
    { open: '"', close: '"', notIn: ['string', 'comment'] },
    { open: '/*', close: '*/', notIn: ['string'] },
  ],
  surroundingPairs: [
    { open: '{', close: '}' },
    { open: '[', close: ']' },
    { open: '(', close: ')' },
    { open: '"', close: '"' },
  ],
}

export const openscadTokens: Monaco.languages.IMonarchLanguage = {
  defaultToken: '',
  keywords: KEYWORDS,
  builtins: BUILTINS,
  tokenizer: {
    root: [
      // `include <lib.scad>` / `use <lib.scad>` — the path is not an operator pair.
      [/\b(include|use)\b(\s*)(<[^>\n]*>)/, ['keyword', '', 'string']],
      // `$fn`, `$fa`, `$fs` and friends are special variables, not identifiers.
      [/\$[a-zA-Z_]\w*/, 'variable.predefined'],
      [
        /[a-zA-Z_]\w*/,
        { cases: { '@keywords': 'keyword', '@builtins': 'type.identifier', '@default': 'identifier' } },
      ],
      { include: '@whitespace' },
      [/[{}()[\]]/, '@brackets'],
      [/\d*\.\d+(?:[eE][-+]?\d+)?/, 'number.float'],
      [/\d+/, 'number'],
      [/"/, { token: 'string.quote', bracket: '@open', next: '@string' }],
      [/[=<>!]=?|[-+*/%]|&&|\|\||[?:]/, 'operator'],
      [/[;,.]/, 'delimiter'],
    ],

    whitespace: [
      [/[ \t\r\n]+/, ''],
      [/\/\*/, 'comment', '@comment'],
      [/\/\/.*$/, 'comment'],
    ],

    comment: [
      [/[^/*]+/, 'comment'],
      [/\*\//, 'comment', '@pop'],
      [/[/*]/, 'comment'],
    ],

    string: [
      [/[^\\"]+/, 'string'],
      [/\\./, 'string.escape'],
      [/"/, { token: 'string.quote', bracket: '@close', next: '@pop' }],
    ],
  },
}

/** Colours taken from the app's own tokens, so the editor is not a second theme. */
const RULES = [
  { token: 'comment', foreground: '5d6a7c', fontStyle: 'italic' },
  { token: 'keyword', foreground: 'f2a93b' },
  { token: 'type.identifier', foreground: '9ec5ff' },
  { token: 'variable.predefined', foreground: '9ec5ff', fontStyle: 'italic' },
  { token: 'number', foreground: '4ade80' },
  { token: 'number.float', foreground: '4ade80' },
  { token: 'string', foreground: '4ade80' },
  { token: 'string.quote', foreground: '4ade80' },
  { token: 'operator', foreground: '8895a7' },
]

export const SCADBUDDY_DARK = 'scadbuddy-dark'
export const SCADBUDDY_LIGHT = 'scadbuddy-light'

export function registerOpenscad(monaco: typeof Monaco): void {
  monaco.languages.register({ id: 'openscad', extensions: ['.scad'], aliases: ['OpenSCAD'] })
  monaco.languages.setLanguageConfiguration('openscad', openscadConfiguration)
  monaco.languages.setMonarchTokensProvider('openscad', openscadTokens)

  monaco.editor.defineTheme(SCADBUDDY_DARK, {
    base: 'vs-dark',
    inherit: true,
    rules: RULES,
    colors: {
      'editor.background': '#131822',
      'editor.foreground': '#dde4ee',
      'editorLineNumber.foreground': '#5d6a7c',
      'editorLineNumber.activeForeground': '#8895a7',
      'editor.lineHighlightBackground': '#1a212c',
      'editorGutter.background': '#1a212c',
    },
  })

  monaco.editor.defineTheme(SCADBUDDY_LIGHT, {
    base: 'vs',
    inherit: true,
    rules: RULES.map((rule) =>
      rule.token === 'comment' ? { ...rule, foreground: '808d9d' } : rule,
    ),
    colors: {
      'editor.background': '#ffffff',
      'editor.foreground': '#161b23',
    },
  })
}
