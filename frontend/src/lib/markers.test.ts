import { describe, expect, it } from 'vitest'
import { toMarkers } from './markers'

const SOURCE = 'size = 10;\n  cube([size, size, size)\n'

describe('toMarkers', () => {
  it('spans the line the diagnostic names, from its first character', () => {
    expect(
      toMarkers([{ severity: 'error', message: 'Parser error: syntax error', line: 2 }], SOURCE),
    ).toEqual([
      {
        severity: 8,
        message: 'Parser error: syntax error',
        startLineNumber: 2,
        startColumn: 3,
        endLineNumber: 2,
        endColumn: 26,
        source: 'OpenSCAD',
      },
    ])
  })

  it('maps warnings and traces below errors', () => {
    const markers = toMarkers(
      [
        { severity: 'warning', message: 'unknown module', line: 1 },
        { severity: 'trace', message: "called by 'assert'", line: 1 },
      ],
      SOURCE,
    )
    expect(markers.map((marker) => marker.severity)).toEqual([4, 2])
  })

  it('puts a diagnostic with no line on the first one rather than dropping it', () => {
    const [marker] = toMarkers(
      [{ severity: 'error', message: 'the customizer schema could not be derived' }],
      SOURCE,
    )
    expect(marker?.startLineNumber).toBe(1)
    expect(marker?.endColumn).toBe(11)
  })

  it('does not trust a line number past the end of the source', () => {
    const [marker] = toMarkers([{ severity: 'error', message: 'boom', line: 99 }], SOURCE)
    expect(marker?.startLineNumber).toBe(1)
  })
})
