import { act, renderHook, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { listUnknownTrays, type TagMadeSpool, type UnknownSpoolTray } from '../api/unknownTrays'
import { resetMockState } from '../mocks/handlers'
import { seedUnknownTray } from '../mocks/features/filament/unknownTrays'
import { server } from '../mocks/server'
import { needsYouItems, needsYouNotices, sourceOf } from './needsYou'
import { codeFromScan, prefillNetWeight, unknownSpoolTitle, useTagMadeToasts, useUnknownSpools } from './unknownSpools'
import { dismissToast, getToasts } from './toast'

const tray = (): UnknownSpoolTray => ({
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
  colour_rgba: '#FF0000FF',
  remain_pct: null,
  first_seen_at: '2026-11-26T09:00:00.000000Z',
  seen_at: '2026-11-26T09:30:00.123456Z',
  candidates: [],
})

const made = (id: string): TagMadeSpool => ({ spool_id: id, number: 7, code: 'S-7', printer_name: 'H2C', slot: 'AMS-A slot 3', made_at: '2026-11-26T10:00:00.000000Z' })

describe('unknown spools (#7143)', () => {
  beforeEach(() => resetMockState())

  it.each([
    ['S-142', 'S-142'],
    [' s142 ', 'S-142'],
    ['https://sb.example/x/S-142', 'S-142'],
    ['https://sb.example/x/S-142?from=label#top', 'S-142'],
    ['/x/s-142', 'S-142'],
    ['L-3', null],
    ['https://sb.example/x/L-3', null],
    ['hello', null],
    ['', null],
  ])('codeFromScan(%j) is %j', (text, code) => expect(codeFromScan(text)).toBe(code))

  it('prefills the net weight from the remain percent and the label weight', () => {
    expect(prefillNetWeight(50, 1000)).toBe(500)
    expect(prefillNetWeight(33, 1000)).toBe(330)
    expect(prefillNetWeight(null, 1000)).toBeNull()
    expect(prefillNetWeight(50, null)).toBeNull()
    expect(prefillNetWeight(undefined, undefined)).toBeNull()
  })

  it('names the tray by its printer and slot', () => {
    expect(unknownSpoolTitle(tray())).toBe('Unknown spool in H2C AMS-A slot 2')
  })

  it('lists an unknown spool by its first sight, from the filament source', () => {
    const one = tray()
    const items = needsYouItems([], null, [], [one])
    expect(items).toEqual([{ kind: 'unknown_spool', key: 'unknown_spool:t1', since: one.first_seen_at, tray: one }])
    expect(sourceOf(items[0]!)).toBe('filament')
    expect(needsYouNotices(items)).toEqual([
      { id: 'unknown_spool:t1', sessionId: null, reason: 'unknown_spool', message: 'Unknown spool in H2C AMS-A slot 2', href: '/activity#needs-you' },
    ])
  })

  it('reads a 404 from the list route as an empty answer', async () => {
    server.use(http.get('/api/v1/filament/unknown-trays', () => HttpResponse.json({ title: 'Not Found', status: 404, detail: 'not found' }, { status: 404 })))
    expect(await listUnknownTrays()).toEqual({ trays: [], made_from_tags: [], served: false })
  })

  it('tells an empty list the server gave from an area that is off', async () => {
    expect((await listUnknownTrays()).served).toBe(true)
  })

  it('reads the trays at once, and keeps the last answer when a read fails', async () => {
    seedUnknownTray()
    const { result } = renderHook(() => useUnknownSpools())
    await waitFor(() => expect(result.current.data?.trays).toHaveLength(1))
    server.use(http.get('/api/v1/filament/unknown-trays', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    const failed = new Promise<void>((resolve) => {
      const done = ({ response }: { response: Response }) => {
        if (response.status !== 500) return
        server.events.removeListener('response:mocked', done)
        resolve()
      }
      server.events.on('response:mocked', done)
    })
    await act(async () => result.current.refresh())
    await failed
    // Let the hook handle the answer before the check.
    await act(async () => {})
    expect(result.current.data?.trays).toHaveLength(1)
  })

  it('toasts a spool made from a tag only when it appears after the first answer', () => {
    for (const one of getToasts()) dismissToast(one.id)
    const { rerender } = renderHook(({ list }) => useTagMadeToasts(list), { initialProps: { list: null as readonly TagMadeSpool[] | null } })
    rerender({ list: [made('a')] })
    expect(getToasts()).toHaveLength(0)
    rerender({ list: [made('a'), made('b')] })
    const shown = getToasts()
    expect(shown).toHaveLength(1)
    expect(shown[0]).toMatchObject({ title: 'Spool S-7 made from its RFID tag', detail: 'H2C AMS-A slot 3', actions: [{ label: 'Open', href: '/filament/spools/b' }] })
    rerender({ list: [made('a'), made('b')] })
    expect(getToasts()).toHaveLength(1)
    for (const one of getToasts()) dismissToast(one.id)
  })
})
