import { describe, expect, it } from 'vitest'
import { seedDraft } from './seed'

// #767 — the form's seed runs in an effect, which can land after the user's first
// keystroke. A seed must never take back what was typed after it was asked for.

describe('seedDraft', () => {
  it('fills the fields it seeds', () => {
    expect(seedDraft({ render_timeout: '60' }, { render_timeout: '300' }, new Set())).toEqual({
      render_timeout: '300',
    })
  })

  it('keeps a field edited since the seed was asked for', () => {
    // The flake: the field was cleared, then the seed put 300 back, and typing 45 made 30045.
    const edited = new Set(['render_timeout'] as const)
    expect(seedDraft({ render_timeout: '' }, { render_timeout: '300', public_url: 'https://a' }, edited)).toEqual({
      render_timeout: '',
      public_url: 'https://a',
    })
  })

  it('leaves the fields it does not seed alone', () => {
    expect(seedDraft({ display_unit: 'in' }, { render_timeout: '300' }, new Set())).toEqual({
      display_unit: 'in',
      render_timeout: '300',
    })
  })
})
