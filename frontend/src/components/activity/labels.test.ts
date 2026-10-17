import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { SUBJECT_NAME_CHARS, subjectLabel } from './labels'

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

describe('the name clip (#3856)', () => {
  it("is the backend title's, so a chip and its row's title show as much", () => {
    const backend = readFileSync(`${import.meta.dirname}/../../../../backend/scadbuddy/operations/activity.py`, 'utf8')
    expect(Number(/^TITLE_NAME_CHARS = (\d+)$/m.exec(backend)?.[1])).toBe(SUBJECT_NAME_CHARS)
  })
})
