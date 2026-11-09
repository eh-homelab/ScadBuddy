import { describe, expect, it } from 'vitest'
import { prints } from '../../mocks/fixtures'
import type { ScadBuddyJob } from '../../api/types'
import { coverSlides, scadbuddyJobPath } from './prints'

describe('coverSlides (#2355)', () => {
  it('opens the cover whole, not the card’s small copy of it', () => {
    const done = prints.find((print) => print.archive_id === 35)!
    expect(done.cover?.url).toContain('size=card')
    expect(coverSlides(done)).toEqual([
      expect.objectContaining({ src: '/api/v1/prints/35/photos/finish_20260927_015703_93372185.jpg' }),
    ])
  })
})

describe('scadbuddyJobPath (#3743)', () => {
  const job = (fields: Partial<ScadBuddyJob>): ScadBuddyJob => ({
    subject: 'output:luna-0002',
    output_id: 'luna-0002',
    library_file_id: null,
    slug: 'keychain',
    run_id: 'run-luna-2',
    archive_id: null,
    matched_by: 'queue_item',
    ...fields,
  })

  it('opens the print once Bambuddy archived it', () => {
    expect(scadbuddyJobPath(job({ archive_id: 35 }))).toBe('/prints/35')
  })

  it("opens the output's page until then", () => {
    expect(scadbuddyJobPath(job({}))).toBe('/edit/luna-0002')
  })

  it("opens a library file's page in the Library", () => {
    expect(
      scadbuddyJobPath(job({ subject: 'library:1630', output_id: null, slug: null, library_file_id: 1630 })),
    ).toBe('/library?file=1630')
  })
})
