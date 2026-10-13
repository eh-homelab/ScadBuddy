import { describe, expect, it } from 'vitest'
import { highlight, languageOf, type Language, type Token } from './highlight'

const kinds = (tokens: Token[]) => tokens.filter((t) => t.kind !== 'plain').map((t) => [t.kind, t.text])

describe('languageOf', () => {
  it('names a language by the file type, and plain text otherwise', () => {
    expect(languageOf('.claude-plugin/plugin.json')).toBe('json')
    expect(languageOf('skills/a/SKILL.md')).toBe('markdown')
    expect(languageOf('hooks/run.sh')).toBe('shell')
    expect(languageOf('server.py')).toBe('python')
    expect(languageOf('index.mjs')).toBe('javascript')
    expect(languageOf('tool.ts')).toBe('javascript')
    expect(languageOf('model.scad')).toBe('openscad')
    expect(languageOf('config.yml')).toBe('yaml')
    expect(languageOf('LICENSE')).toBe('plain')
    expect(languageOf('README', 'text/markdown')).toBe('markdown')
  })
})

describe('highlight', () => {
  it('never changes the text: the tokens join back to it', () => {
    const samples: [Language, string][] = [
      ['json', '{"a": [1, -2.5e3, true, null, "x\\"y"]}\n'],
      ['markdown', '---\nname: x\n---\n\n# Title\n\nSome `code` here.\n\n```sh\nrm -rf /\n```\n'],
      ['shell', '#!/bin/sh\nif [ -n "$X" ]; then echo \'hi\' # done\nfi\n'],
      ['python', 'def f(x):\n    """doc"""\n    return x + 1  # one\n'],
      ['javascript', "const a = `t`; // c\n/* block */ function f() { return 'x' }\n"],
      ['openscad', 'module m(h = 2) { cube([1, 2, h]); } // c\n'],
      ['yaml', 'key: "v" # c\nlist:\n  - 1\n'],
      ['plain', 'anything at all\n'],
    ]
    for (const [language, text] of samples) {
      expect(highlight(text, language).map((t) => t.text).join(''), language).toBe(text)
    }
  })

  it('marks JSON keys, strings, numbers and literals', () => {
    expect(kinds(highlight('{"name": "greeter", "n": 2, "ok": true}', 'json'))).toEqual([
      ['punct', '{'],
      ['key', '"name"'],
      ['punct', ':'],
      ['string', '"greeter"'],
      ['punct', ','],
      ['key', '"n"'],
      ['punct', ':'],
      ['number', '2'],
      ['punct', ','],
      ['key', '"ok"'],
      ['punct', ':'],
      ['keyword', 'true'],
      ['punct', '}'],
    ])
  })

  it('marks Markdown frontmatter, headings, inline code and fenced code', () => {
    const tokens = kinds(highlight('---\nname: hi\n---\n# Hello\nRun `ls` now.\n```\nx = 1\n```\n', 'markdown'))
    expect(tokens).toEqual([
      ['meta', '---\nname: hi\n---\n'],
      ['heading', '# Hello'],
      ['string', '`ls`'],
      ['string', '```\nx = 1\n```\n'],
    ])
  })

  it('marks comments, strings and keywords in code, but not a keyword inside a word', () => {
    expect(kinds(highlight('if x; then echo "a" # note', 'shell'))).toEqual([
      ['keyword', 'if'],
      ['keyword', 'then'],
      ['string', '"a"'],
      ['comment', '# note'],
    ])
    expect(kinds(highlight('modules = 1', 'openscad'))).toEqual([['number', '1']])
  })
})
