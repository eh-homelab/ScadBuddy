import { StringStream } from '@codemirror/language'
import { describe, expect, it } from 'vitest'
import { openscad } from './scadMode'

/** Runs the mode over one line and returns its `[token, text]` pairs. */
function tokens(line: string, state = openscad.startState!(2)) {
  const stream = new StringStream(line, 2, 2)
  const out: [string | null, string][] = []
  while (!stream.eol()) {
    const token = openscad.token(stream, state)
    const text = stream.current()
    stream.start = stream.pos
    if (text.trim()) out.push([token, text])
  }
  return out
}

describe('the OpenSCAD mode', () => {
  it('colours declarations, builtins and numbers apart', () => {
    expect(tokens('module tag(width = 10) cube([width, 1, 1]);')).toEqual([
      ['keyword', 'module'],
      ['variable', 'tag'],
      [null, '('],
      ['variable', 'width'],
      ['operator', '='],
      ['number', '10'],
      [null, ')'],
      ['builtin', 'cube'],
      [null, '('],
      [null, '['],
      ['variable', 'width'],
      [null, ','],
      ['number', '1'],
      [null, ','],
      ['number', '1'],
      [null, ']'],
      [null, ')'],
      [null, ';'],
    ])
  })

  it('reads a customizer annotation as a comment', () => {
    expect(tokens('width = 10; // [1:100]')).toContainEqual(['comment', '// [1:100]'])
  })

  it('reads special variables and strings', () => {
    expect(tokens('$fn = 64; name = "Ada";')).toContainEqual(['special', '$fn'])
    expect(tokens('name = "Ada";')).toContainEqual(['string', '"Ada"'])
  })

  it('keeps a block comment open across lines', () => {
    const state = openscad.startState!(2)
    expect(tokens('/* [Main]', state).map(([token]) => token)).toEqual(['comment', 'comment'])
    expect(state.inBlockComment).toBe(true)
    expect(tokens('still a comment */ cube(1);', state)).toContainEqual(['builtin', 'cube'])
  })

  it('reads an include path as a string', () => {
    expect(tokens('include <lib/helpers.scad>')).toContainEqual(['string', '<lib/helpers.scad>'])
  })
})
