import { beforeEach, describe, expect, it } from 'vitest'
import { api, type ApiError } from '../../../api/client'
import { mockSettings, resetMockState, setMockSettings } from '../../handlers'
import { FILAMENT_PLA, ORDER_DRAFT, OUTPUT_REAGAN, PROBLEMS, state } from './state'

// #4777, order operations spec 2026-11-07 §3.2: the mock answers a past print's item and
// its `archive` quote as `api/orders.py` `_archive_item` and `orders/past_prints.py` do.

const failure = (promise: Promise<unknown>) => promise.then(() => undefined, (e: unknown) => e as ApiError)

describe('past prints mock (#4777)', () => {
  beforeEach(() => resetMockState())

  it('lists the archives with the route filters', async () => {
    const all = await api.listFarmArchives({}, { limit: 50 })
    expect(all.map((a) => a.id)).toEqual([9105, 9104, 9103, 9102, 9101])
    expect((await api.listFarmArchives({ printer_id: 2 })).map((a) => a.id)).toEqual([9105])
    expect((await api.listFarmArchives({ project_id: 7 })).map((a) => a.id)).toEqual([9104])
    expect((await api.listFarmArchives({}, { limit: 2, offset: 1 })).map((a) => a.id)).toEqual([9104, 9103])
  })

  it('makes an archive the item of what printed it, else of its native copy', async () => {
    const model = await api.addOrderItem(ORDER_DRAFT, { archive_id: 9102, quantity: 1 })
    expect([model.kind, model.output_id, model.origin_archive_ids]).toEqual(['model', OUTPUT_REAGAN, [9101, 9102]])
    const file = await api.addOrderItem(ORDER_DRAFT, { archive_id: 9103, quantity: 1 })
    expect([file.kind, file.library_file?.id, file.origin_archive_ids]).toEqual(['library_file', 120, [9103]])
    const refused = await failure(api.addOrderItem(ORDER_DRAFT, { archive_id: 9104, quantity: 1 }))
    expect(refused?.problem.type).toBe(PROBLEMS + 'archive_copy_not_native')
    setMockSettings({ ...mockSettings(), library_backend: 'native' })
    const copy = await api.addOrderItem(ORDER_DRAFT, { archive_id: 9104, quantity: 1 })
    const again = await api.addOrderItem(ORDER_DRAFT, { archive_id: 9104, quantity: 1 })
    expect([copy.library_file?.library, copy.library_file?.filename]).toEqual(['native', 'benchy.gcode.3mf'])
    expect(again.library_file?.id).toBe(copy.library_file?.id)
    const gone = await failure(api.addOrderItem(ORDER_DRAFT, { archive_id: 1, quantity: 1 }))
    expect(gone?.problem.type).toBe(PROBLEMS + 'archive_gone')
    const mixed = await failure(api.addOrderItem(ORDER_DRAFT, { archive_id: 9104, output_id: OUTPUT_REAGAN, quantity: 1 }))
    expect(mixed?.status).toBe(422)
  })

  it('prices an item from its print, every plate of the run, and refuses a failed print', async () => {
    const item = await api.addOrderItem(ORDER_DRAFT, { archive_id: 9101, quantity: 2 })
    const filaments = Object.fromEntries(item.colours.map((c) => [String(c.slot), FILAMENT_PLA]))
    const quote = await api.quoteItemFromArchive(ORDER_DRAFT, item.id, { filaments })
    expect(quote.source).toBe('archive')
    expect(quote.plates.map((p) => [p.index, p.print_seconds])).toEqual([
      [1, 3300],
      [2, 3300],
    ])
    expect(quote.total_minor).toBe(quote.unit_price_minor * 2)
    // A print that is not the item's is refused before anything is read.
    const stranger = await failure(api.quoteItemFromArchive(ORDER_DRAFT, item.id, { archive_id: 9105, filaments }))
    expect(stranger?.status).toBe(422)
  })

  it('gives a library file item with no slots the slots its print used', async () => {
    const item = await api.addOrderItem(ORDER_DRAFT, { archive_id: 9103, quantity: 1 })
    expect(item.colours).toEqual([])
    await api.quoteItemFromArchive(ORDER_DRAFT, item.id, { filament_id: FILAMENT_PLA })
    const after = state.items.find((i) => i.id === item.id)
    expect(after?.colours).toEqual([{ slot: 0, colour: '#D62828', filament_id: FILAMENT_PLA }])
    expect(after?.status).toBe('quoted')
  })
})
