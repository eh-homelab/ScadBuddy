import { describe, expect, it } from 'vitest'
import { prints } from '../../mocks/fixtures'
import { coverSlides } from './prints'

describe('coverSlides (#2355)', () => {
  it('opens the cover whole, not the card’s small copy of it', () => {
    const done = prints.find((print) => print.archive_id === 35)!
    expect(done.cover?.url).toContain('size=card')
    expect(coverSlides(done)).toEqual([
      expect.objectContaining({ src: '/api/v1/prints/35/photos/finish_20260927_015703_93372185.jpg' }),
    ])
  })
})
