import { describe, expect, it } from 'vitest'
import { detectLibraries } from './libraryImports'

const CATALOGUE = ['BOSL2', 'dotSCAD']

describe('detectLibraries', () => {
  it('names the curated libraries a use or include opens, once each, in order', () => {
    const source = [
      'include <dotSCAD/src/shape_circle.scad>',
      'use <BOSL2/std.scad>',
      'include <BOSL2/gears.scad>',
      'cube(1);',
    ].join('\n')

    expect(detectLibraries(source, CATALOGUE)).toEqual(['dotSCAD', 'BOSL2'])
  })

  it('ignores files beside the model and libraries outside the catalogue', () => {
    const source = 'use <parts.scad>\nuse <MCAD/gears.scad>\ninclude <bosl2/std.scad>\n'

    expect(detectLibraries(source, CATALOGUE)).toEqual([])
  })

  it('ignores commented-out imports', () => {
    const source = '// use <BOSL2/std.scad>\n/* include <dotSCAD/x.scad> */\ncube(1);\n'

    expect(detectLibraries(source, CATALOGUE)).toEqual([])
  })

  it('treats everything after an unterminated block comment as commented', () => {
    const source = 'use <dotSCAD/a.scad>\n/* half-typed\nuse <BOSL2/std.scad>\n'

    expect(detectLibraries(source, CATALOGUE)).toEqual(['dotSCAD'])
  })

  it('reads spacing and several statements on one line', () => {
    const source = 'use < BOSL2/std.scad >  include<dotSCAD/a.scad>'

    expect(detectLibraries(source, CATALOGUE)).toEqual(['BOSL2', 'dotSCAD'])
  })
})
