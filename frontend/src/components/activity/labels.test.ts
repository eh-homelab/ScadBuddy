import { describe, expect, it } from 'vitest'
import { subjectLabel } from './labels'

describe('subjectLabel (#3856)', () => {
  it("prefers the server's name for the subject", () => {
    expect(subjectLabel({ value: 'keychain', label: 'Name Keychain' })).toBe('Name Keychain')
  })

  it.each([
    ['library:89', 'Library file 89'],
    ['library:BOSL2', 'Library BOSL2'],
    // #4001: an OpenSCAD library's own prefix, so an all-digit name is no file.
    ['scad-library:BOSL2', 'Library BOSL2'],
    ['scad-library:123', 'Library 123'],
    ['library-folder:4', 'Library folder 4'],
    ['library-files', 'Library files'],
    ['project:7', 'Project 7'],
    ['project-name:Reagan keychains', 'Project “Reagan keychains”'],
    ['project-name:a:b', 'Project “a:b”'],
    [`project-name:${'x'.repeat(50)}`, `Project “${'x'.repeat(40)}…”`],
    [`output:${'a'.repeat(32)}`, 'Output aaaaaaaa'],
    ['keychain', 'keychain'],
    ['unknown:thing', 'unknown:thing'],
    ['library:', 'library:'],
  ])('reads %s as %s', (value, label) => {
    expect(subjectLabel({ value, label: null }, 40)).toBe(label)
  })
})

describe('the name clip (#4003)', () => {
  it('is the count the facets give, and none before they load', () => {
    const value = `project-name:${'x'.repeat(12)}`
    expect(subjectLabel({ value }, 5)).toBe('Project “xxxxx…”')
    expect(subjectLabel({ value }, 12)).toBe(`Project “${'x'.repeat(12)}”`)
    expect(subjectLabel({ value })).toBe(`Project “${'x'.repeat(12)}”`)
  })
})
