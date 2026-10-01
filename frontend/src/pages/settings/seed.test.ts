import { describe, expect, it } from 'vitest'
import type { FieldName } from './fields'
import { editedSince, pendingFields, seedDraft, type Edits } from './seed'

// #767 — a seed must never take back what was typed after its request was sent: the
// seed's effect can land after a keystroke, a save's answer can arrive after more
// typing, and several requests can be in flight at once.

const counts = (entries: [FieldName, number][]): Edits => new Map(entries)

describe('seedDraft', () => {
  it('fills the fields it seeds', () => {
    expect(seedDraft({ render_timeout: '60' }, { render_timeout: '300' }, new Set())).toEqual({
      render_timeout: '300',
    })
  })

  it('keeps a field edited since the seed was asked for', () => {
    // The flake: the field was cleared, then the seed put 300 back, and typing 45 made 30045.
    const edited = new Set<FieldName>(['render_timeout'])
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

describe('pendingFields and editedSince', () => {
  it('counts an edit typed after the request was sent', () => {
    const since = pendingFields([{ names: ['render_timeout'], since: counts([['render_timeout', 1]]) }], () => [])
    expect(editedSince(since, counts([['render_timeout', 2]]))).toEqual(new Set(['render_timeout']))
    expect(editedSince(since, counts([['render_timeout', 1]]))).toEqual(new Set())
  })

  it('expands a whole-form seed to every field', () => {
    const since = pendingFields([{ names: 'all', since: counts([]) }], () => ['render_timeout', 'public_url'])
    expect([...since.keys()]).toEqual(['render_timeout', 'public_url'])
    expect(editedSince(since, counts([['public_url', 1]]))).toEqual(new Set(['public_url']))
  })

  it('does not let a second request forget an edit made during the first (review of #911)', () => {
    // Save A sent at count 1; the field is edited again (2); save B is sent and answers.
    // Each request carries its own snapshot, so B cannot erase the edit A must keep.
    const saveA = { names: ['render_timeout'] as FieldName[], since: counts([['render_timeout', 1]]) }
    const saveB = { names: ['display_unit'] as FieldName[], since: counts([['render_timeout', 2]]) }
    const since = pendingFields([saveB, saveA], () => [])
    expect(editedSince(since, counts([['render_timeout', 2]]))).toEqual(new Set(['render_timeout']))
  })

  it('takes the later request when two seed one field', () => {
    const since = pendingFields(
      [
        { names: ['render_timeout'], since: counts([['render_timeout', 3]]) },
        { names: ['render_timeout'], since: counts([['render_timeout', 1]]) },
      ],
      () => [],
    )
    expect(since.get('render_timeout')).toBe(3)
  })
})
