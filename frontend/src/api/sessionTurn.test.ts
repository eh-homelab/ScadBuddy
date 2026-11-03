import { describe, expect, it } from 'vitest'
import { oneOf, SYSTEM_PROMPT_BASES, TOOL_SEARCH_MODES } from './sessionTurn'

describe('oneOf', () => {
  it('keeps a value the agent takes and drops any other', () => {
    expect(oneOf(TOOL_SEARCH_MODES, 'always')).toBe('always')
    expect(oneOf(SYSTEM_PROMPT_BASES, 'minimal')).toBe('minimal')
    expect(oneOf(TOOL_SEARCH_MODES, 'sometimes')).toBeUndefined()
    expect(oneOf(SYSTEM_PROMPT_BASES, '')).toBeUndefined()
  })
})
