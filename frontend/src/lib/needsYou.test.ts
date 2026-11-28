import { describe, expect, it } from 'vitest'
import type { Spool } from '../api/filament'
import type { UnknownSpoolTray } from '../api/unknownTrays'
import { needsYouItems, needsYouNotices, sourceOf } from './needsYou'

const tray = (first_seen_at: string): UnknownSpoolTray => ({
  id: 't1',
  printer_id: 'p1',
  printer_name: 'H2C',
  ams_id: 0,
  tray_id: 1,
  slot: 'AMS-A slot 2',
  place_id: null,
  place_name: null,
  place_code: null,
  material: 'PLA',
  colour_rgba: null,
  remain_pct: null,
  first_seen_at,
  seen_at: first_seen_at,
  candidates: [],
})

const waiting = (id: string, number: number, home_asked_at: string) =>
  ({ id, number, home_location_id: 'box-1', home_asked_at, location_id: null }) as unknown as Spool

describe('needsYouItems: the home return (#7167)', () => {
  it('lists a spool asked about its dry box by home_asked_at, among the others', () => {
    const early = waiting('s12', 12, '2026-11-26T08:00:00Z')
    const late = waiting('s13', 13, '2026-11-26T10:00:00Z')
    const items = needsYouItems([], null, [], [tray('2026-11-26T09:00:00Z')], [late, early])
    expect(items.map((item) => item.key)).toEqual(['home-return:s12', 'unknown_spool:t1', 'home-return:s13'])
    expect(items[0]).toEqual({ kind: 'home-return', key: 'home-return:s12', since: '2026-11-26T08:00:00Z', spool: early })
    expect(sourceOf(items[0]!)).toBe('homes')
  })

  it('raises one notice per spool, opening Needs you', () => {
    const items = needsYouItems([], null, [], [], [waiting('s12', 12, '2026-11-26T08:00:00Z')])
    expect(needsYouNotices(items)).toEqual([
      { id: 'home-return:s12', sessionId: null, reason: 'home_return', message: 'S-12 came out of its tray. Which spool goes into its dry box?', href: '/activity#needs-you' },
    ])
  })
})
