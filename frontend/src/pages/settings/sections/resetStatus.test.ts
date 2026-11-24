import { describe, expect, it } from 'vitest'
import type { LinksResetDone } from '../../../api/filament'
import { statusAfterKeptReset } from './resetStatus'

const status = { instance: '0ld0ld0ld0ld', instance_changed: true }
const counts = { spools: 0, suppliers: 0, locations: 0, insert_types: 0, filaments: 0, colours: 0, presets: 0, intents: 0, errors: 0, ignored: 0 }

describe('statusAfterKeptReset (#6845)', () => {
  it('shows the instance the kept reset recorded, and no longer a changed one', () => {
    const done: LinksResetDone = { instance: '0ld0ld0ld0ld', kept: true, configured_instance: 'n3wn3wn3wn3w', deleted: counts }
    const next = statusAfterKeptReset(status, done)
    expect(next.instance).toBe('n3wn3wn3wn3w')
    expect(next.instance_changed).toBe(false)
  })

  it('keeps the old instance when the answer names none', () => {
    const done: LinksResetDone = { instance: '0ld0ld0ld0ld', kept: true, configured_instance: null, deleted: counts }
    expect(statusAfterKeptReset(status, done).instance).toBe('0ld0ld0ld0ld')
  })
})
