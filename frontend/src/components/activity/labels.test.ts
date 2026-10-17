import { describe, expect, it } from 'vitest'
import { subjectLabel } from './labels'

describe('subjectLabel (#3856)', () => {
  it("prefers the server's name for the subject", () => {
    expect(subjectLabel({ value: 'keychain', label: 'Name Keychain' })).toBe('Name Keychain')
  })

  it.each([
    ['library:89', 'Library file 89'],
    ['library:BOSL2', 'Library BOSL2'],
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
    expect(subjectLabel({ value, label: null })).toBe(label)
  })
})
