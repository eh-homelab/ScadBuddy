import { describe, expect, it } from 'vitest'
import { browserOptions, browserProblem, EMPTY_BROWSER } from './browserDraft'

const draft = (width: string, height: string) => ({ ...EMPTY_BROWSER, width, height })

describe('browserProblem (#7027)', () => {
  it('takes digits only as a side', () => {
    expect(browserProblem(draft('1280', '800'))).toBeNull()
    expect(browserOptions(draft(' 1280 ', '800'))).toEqual({ viewport: { width: 1280, height: 800 } })
  })

  it.each([['1e3'], ['0x200'], ['1.0'], ['-5'], ['12px']])('holds %s, which Number() would read as a number', (side) => {
    expect(browserProblem(draft(side, '800'))).toBe('Width and height are whole numbers of pixels.')
    expect(browserProblem(draft('800', side))).toBe('Width and height are whole numbers of pixels.')
  })

  it('holds one side without the other', () => {
    expect(browserProblem(draft('1280', ''))).toBe('Set both width and height, or neither.')
  })
})
