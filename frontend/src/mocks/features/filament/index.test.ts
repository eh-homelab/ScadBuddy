import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, request } from '../../../api/client'
import {
  addSpoolEvent,
  archiveSpool,
  bindTag,
  bulkArchiveSpools,
  bulkCreateSpools,
  bulkUpdateSpools,
  closingPreview,
  copySpool,
  createLabelSheet,
  createSpool,
  deleteSpool,
  getForecast,
  getInventory,
  getMetrics,
  getSpool,
  getSpoolHistory,
  getSummary,
  getTag,
  getType,
  importSpools,
  insertWeightOffer,
  listCatalog,
  listEvents,
  listLocations,
  listSensors,
  listSpoolEvents,
  listSpools,
  listTags,
  moveSpool,
  patchSpool,
  unbindTag,
  resolveCatalog,
  setCatalogAliases,
  startColourSync,
  upsertCatalog,
  upsertLocation,
  upsertType,
  type SpoolCreate,
  type SpoolPatch,
  type SpoolTypeCreate,
} from '../../../api/filament'
import { resetMockState } from '../../handlers'
import { seedHomeReturn } from './index'
import { B, H2C, IT, L, LK, M, SENSOR, spoolId, T, TAG } from './seed'

// #6262 Task 14 — the Filament section's mock API over its seed, through the client.

/** The problem a call was refused with: its status, title and detail. */
async function refusal(call: Promise<unknown>) {
  const error = await call.then(
    () => null,
    (cause: unknown) => cause,
  )
  expect(error).toBeInstanceOf(ApiError)
  // The problem without its `type`: a refusal's extensions (#6706's holder) come along.
  const rest: Record<string, unknown> = { ...(error as ApiError).problem }
  delete rest.type
  return rest
}

const NEW_SPOOL = { weight_locked: false } as SpoolCreate
const NOBODY = '00000000-0000-4000-8000-000000000000'

describe('the filament mocks', () => {
  it('seeds 12 spools: opaque, translucent, clear, one needing a type, one Unknown, one archived', async () => {
    const all = await listSpools({ includeArchived: true })
    expect(all).toHaveLength(12)
    const live = await listSpools()
    expect(live.map((spool) => spool.number)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
    const rgba = (number: number) => all.find((spool) => spool.number === number)?.effective?.rgba
    expect(rgba(1)).toBe('#FFFFFFFF')
    expect(rgba(2)).toBe('#FF6A1380')
    expect(rgba(5)).toBe('#FFFFFF10')
    expect((await listSpools({ needsType: true })).map((spool) => spool.number)).toEqual([6, 7])
    expect(all.find((spool) => spool.number === 7)?.names?.material).toBe('Unknown')
    expect(all.find((spool) => spool.number === 4)?.low_stock).toBe(true)
  })

  it("keeps the archived spool's last tag, now on spool #3", async () => {
    const archived = await getSpool(spoolId(19))
    expect(archived.archived_at).not.toBeNull()
    expect(archived.tag).toBeNull()
    expect(archived.last_tag).toMatchObject({ tag: { id: TAG.MOVED }, held_by: 3 })
    expect((await getSpool(spoolId(3))).tag?.id).toBe(TAG.MOVED)
    const history = (await getTag(TAG.MOVED)).history ?? []
    expect(history.map((binding) => binding.spool_id)).toEqual([spoolId(3), spoolId(19)])
    expect((await listTags()).map((tag) => tag.spool_number)).toEqual([3, 4])
    expect(await listTags({ includeUnbound: true })).toHaveLength(3)
  })

  it('serves the inventory, the summary and the places from the same spools', async () => {
    const inventory = await getInventory()
    expect(inventory.spools.find((row) => row.spool.number === 8)?.loaded).toMatchObject({ ams_id: 0, tray_id: 3 })
    expect(inventory.spools.some((row) => row.spool.number === 19)).toBe(false)
    expect(await getSummary()).toMatchObject({ spools: 11, loaded: 2, needs_type: 2 })
    const [workshop] = await listLocations()
    expect((workshop?.children ?? []).map((place) => [place.name, place.spool_count])).toEqual([
      ['Shelf A', 2],
      ['Dry box 1', 2],
    ])
  })

  it("counts the summary's consumed grams by the one plate rule the history uses (metrics.plates)", async () => {
    // #2's three plates (120 g) and #3's job SB and BB both booked, once (20 g). #4's `print` ref and #10's open estimate are no plate.
    expect((await getSummary()).consumed_g).toBe(140)
    const used = await Promise.all([2, 3, 4, 10].map(async (number) => (await getSpoolHistory(spoolId(number))).metrics.used_g))
    expect(used).toEqual([120, 20, 0, 0])
  })

  it('values the stock at the exact grams, as metrics_store._value does (R-T8-9)', async () => {
    // A spool of the orange type (19.99/kg) with 823.5 g, beside #2's 820 g. The backend's own
    // `_value` and `_money` on these two spools give 16.3918 + 16.461765 = 32.853565, so 32.85;
    // rounding the grams first (824 g) would give 32.86.
    await createSpool({ ...NEW_SPOOL, type_id: T.ORANGE, grams_left: 823.5 })
    const metrics = await getMetrics({ dimension: 'type' })
    expect(metrics.stock.find((row) => row.key === T.ORANGE)).toMatchObject({ spools: 2, grams_left: 1643.5, value: '32.85' })
  })

  it('does not call a type with no spool in stock low', async () => {
    const spool = await createSpool({ ...NEW_SPOOL, type_id: T.JADE, grams_left: 1000 })
    for (const each of await listSpools({ includeArchived: true })) {
      if (each.type_id === T.JADE) await archiveSpool(each.id)
    }
    expect(spool.type_id).toBe(T.JADE)
    const found = (await getForecast()).types.find((type) => type.type_id === T.JADE)
    expect(found).toMatchObject({ spools: 0, low_stock: false })
  })

  it('allows a single day, since equal to until, and refuses since after until', async () => {
    const metrics = await getMetrics({ since: '2026-11-10', until: '2026-11-10' })
    expect(metrics.since).toBe('2026-11-10')
    expect((await refusal(getMetrics({ since: '2026-11-11', until: '2026-11-10' }))).status).toBe(422)
  })

  it('pages the ledger with no row lost or repeated, and refuses a cursor no page gave', async () => {
    const seen: string[] = []
    let cursor: string | null | undefined
    do {
      const page = await listEvents({ limit: 5, cursor })
      seen.push(...page.events.map((event) => event.id))
      cursor = page.next_cursor
    } while (cursor)
    const all = await listEvents({ limit: 500 })
    expect(new Set(seen).size).toBe(seen.length)
    expect(seen).toEqual(all.events.map((event) => event.id))
    expect(await refusal(listEvents({ cursor: 'nope' }))).toEqual({
      status: 422,
      title: 'Unprocessable Content',
      detail: 'the cursor is not one this ledger gave',
    })
  })

  it('changes a spool, and reset puts the seed back', async () => {
    await patchSpool(spoolId(1), { rgba: '#ff000080' } as SpoolPatch)
    expect((await getSpool(spoolId(1))).effective?.rgba).toBe('#FF000080')
    await archiveSpool(spoolId(1))
    expect(await listSpools()).toHaveLength(10)
    resetMockState()
    expect((await getSpool(spoolId(1))).effective?.rgba).toBe('#FFFFFFFF')
    expect(await listSpools()).toHaveLength(11)
  })

  it('keeps a weighing\'s breakdown and the spool\'s last drift when it has the reading (#7682)', async () => {
    await addSpoolEvent(spoolId(1), { kind: 'weighed', source: 'scale', grams: 700 })
    expect((await getSpool(spoolId(1))).last_drift_g).toBeNull()
    const weighed = await addSpoolEvent(spoolId(1), { kind: 'weighed', source: 'scale', reading_g: 940 })
    expect(weighed.grams_after).toBe(690)
    expect(weighed.data).toMatchObject({ reading_g: 940, empty_g: 250, empty_source: 'insert', empty_name: 'Bambu reusable spool', expected_g: 700, drift_g: -10 })
    const spool = await getSpool(spoolId(1))
    expect(spool.last_drift_g).toBe(-10)
    expect(spool.drift_notice).toBe(true)
    expect(spool.last_weighing?.id).toBe(weighed.id)
    const byHand = await addSpoolEvent(spoolId(1), { kind: 'weighed', reading_g: 900, grams: 640 })
    expect(byHand.data).toMatchObject({ empty_g: 260, empty_source: 'person', empty_basis: null, empty_name: null })
    await addSpoolEvent(spoolId(1), { kind: 'weighed', grams: 600 })
    expect((await getSpool(spoolId(1))).last_drift_g).toBeNull()
    const bare = await createSpool({ ...NEW_SPOOL, material_id: M.PLA, label_weight_g: 1000 })
    expect(await refusal(addSpoolEvent(bare.id, { kind: 'weighed', reading_g: 900 }))).toMatchObject({ status: 422, code: 'no_empty_weight' })
    expect(await refusal(addSpoolEvent(spoolId(1), { kind: 'weighed', reading_g: 100 }))).toMatchObject({ status: 422, code: 'reading_below_empty' })
  })

  it('answers 404 for a spool, type or tag that does not exist', async () => {
    expect(await refusal(getSpool(NOBODY))).toEqual({ status: 404, title: 'Not Found', detail: `no spool ${NOBODY}` })
    expect(await refusal(getType(NOBODY))).toMatchObject({ status: 404, title: 'Not Found' })
    expect(await refusal(unbindTag(NOBODY))).toMatchObject({ status: 404, title: 'Not Found' })
  })

  it('adds a spool with a type, or with only its material, and refuses one with neither', async () => {
    const typed = await createSpool({ ...NEW_SPOOL, type_id: T.JADE, grams_left: 1000 })
    expect(typed).toMatchObject({ number: 20, type_id: T.JADE, effective: { rgba: '#FFFFFFFF' }, names: { material: 'PLA' } })
    const untyped = await createSpool({ ...NEW_SPOOL, material_id: M.PETG, rgba: '#00ff0080' })
    expect(untyped).toMatchObject({ number: 21, type_id: null, rgba: '#00FF0080' })
    expect((await listSpools({ needsType: true })).map((spool) => spool.number)).toContain(21)

    expect(await refusal(createSpool(NEW_SPOOL))).toEqual({
      status: 422,
      title: 'Unprocessable Content',
      detail: 'a spool needs a type_id or a material_id',
    })
    expect(await refusal(createSpool({ ...NEW_SPOOL, material_id: M.PLA, brand_id: T.JADE }))).toMatchObject({
      status: 422,
      detail: 'a row this one points at does not exist',
    })
    expect(await refusal(createSpool({ ...NEW_SPOOL, material_id: M.PLA, nozzle_temp_min: 240, nozzle_temp_max: 200 }))).toMatchObject({
      status: 422,
      detail: 'nozzle_temp_min is above nozzle_temp_max',
    })
    expect(await refusal(patchSpool(spoolId(6), { material_id: null } as SpoolPatch))).toMatchObject({
      status: 422,
      detail: 'a spool needs a type_id or a material_id',
    })
  })

  it('deletes a spool that never printed, and refuses one that did', async () => {
    expect(await refusal(deleteSpool(spoolId(4)))).toEqual({
      status: 409,
      title: 'Conflict',
      detail: 'the spool has prints in its ledger; archive it instead',
    })
    await deleteSpool(spoolId(9))
    expect(await refusal(getSpool(spoolId(9)))).toMatchObject({ status: 404 })
  })

  it('binds a tag, moving it off the spool that had it, and unbinds it', async () => {
    const bound = await bindTag(spoolId(1), { tag_uid: '04:a1:b2:c3:d4:e5:f6', kind: 'other', override: true })
    expect(bound).toMatchObject({ tag_id: TAG.MOVED, spool_id: spoolId(1), unbound_at: null })
    expect((await getSpool(spoolId(3))).tag).toBeNull()
    const history = (await getTag(TAG.MOVED)).history ?? []
    expect(history[1]).toMatchObject({ spool_id: spoolId(3), reason: 'rebound' })

    expect(await unbindTag(TAG.MOVED)).toMatchObject({ spool_id: spoolId(1), reason: 'unbound' })
    expect((await getSpool(spoolId(1))).tag).toBeNull()
    expect(await refusal(unbindTag(TAG.MOVED))).toMatchObject({ status: 404 })
  })

  it('refuses a tag whose two keys name two tags, and one with no key once cleaned', async () => {
    const clash = bindTag(spoolId(1), { tag_uid: '04A1B2C3D4E5F6', tray_uuid: '7F3E1C0A9B8D4E2F8A6B5C4D3E2F1A0B', kind: 'other', override: false })
    expect(await refusal(clash)).toEqual({
      status: 409,
      title: 'Conflict',
      detail: 'the tag_uid and the tray_uuid name two different tags',
    })
    expect(await refusal(bindTag(spoolId(1), { tag_uid: '00:00:00', kind: 'other', override: false }))).toMatchObject({
      status: 422,
      detail: 'the tag has no tag_uid or tray_uuid once cleaned',
    })
  })

  it('archiving a spool takes its tag off', async () => {
    await archiveSpool(spoolId(4))
    expect((await getSpool(spoolId(4))).last_tag).toMatchObject({ tag: { id: TAG.BLACK }, reason: 'archived' })
  })

  it('archives with a reason and a final reading: the weighing, then the closing (#7684)', async () => {
    await addSpoolEvent(spoolId(1), { kind: 'weighed', source: 'scale', grams: 700 })
    const preview = await closingPreview(spoolId(1), 'finished', 280, null)
    expect(preview.final_filament_g).toBe(30)
    expect(preview.closing).toMatchObject({ reason: 'finished', started_from: 'reading', scale_consumed_g: (preview.closing.started_g ?? 0) - 30 })
    expect(preview.notices.map((one) => one.code)).toEqual(['filament_left'])
    expect(preview.notices[0]?.text).toBe('30 g of filament are still on it.')
    expect((await closingPreview(spoolId(1), 'damaged', 253, null)).notices.map((one) => one.code)).toEqual(['not_finished_but_empty'])
    const done = await archiveSpool(spoolId(1), { archived: true, reason: 'finished', final_reading_g: 280 })
    expect(done.archived_at).not.toBeNull()
    // The 30 g left are written off (#7167): the spool has none left.
    expect(done.grams_left).toBe(0)
    const events = await listSpoolEvents(spoolId(1))
    expect(events.slice(-3).map((one) => one.kind)).toEqual(['weighed', 'non_print_waste', 'archived'])
    expect(events.at(-1)?.data).toMatchObject({ reason: 'finished', final_filament_g: 30, scale_consumed_g: (preview.closing.started_g ?? 0) - 30, ledger_consumed_g: preview.closing.ledger_consumed_g })
    expect(await refusal(archiveSpool(spoolId(2), { archived: false, reason: 'finished' }))).toMatchObject({ status: 422 })
    expect(await refusal(archiveSpool(spoolId(2), { archived: true, reason: 'damaged', final_reading_g: 100 }))).toMatchObject({ status: 422, code: 'reading_below_empty' })
    expect((await getSpool(spoolId(2))).archived_at).toBeNull()
  })

  describe('non-print waste (#7167): the mocks book it as the backend does', () => {
    // A fixed clock: the mock stamps the waste with `now()`, and the range below is that day.
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ['Date'] }).setSystemTime('2026-12-01T12:00:00Z')
    })
    afterEach(() => {
      vi.useRealTimers()
    })
    const today = () => '2026-12-01'
    const waste = async () => (await getMetrics({ since: today(), until: today() })).non_print_waste_g
    const events = async (id: string) => (await listSpoolEvents(id)).filter((one) => one.kind === 'non_print_waste')

    it('an archive books the grams left after the final weighing, before the archived event', async () => {
      expect(await waste()).toBe(0)
      await archiveSpool(spoolId(1), { archived: true, reason: 'finished', final_grams: 30 })
      const kinds = (await listSpoolEvents(spoolId(1))).map((one) => one.kind)
      expect(kinds.slice(-3)).toEqual(['weighed', 'non_print_waste', 'archived'])
      expect((await events(spoolId(1)))[0]).toMatchObject({ source: 'person', grams_after: 0, data: { waste_g: 30, cause: 'archive' } })
      expect((await getSpool(spoolId(1))).grams_left).toBe(0)
      const found = await getMetrics({ since: today(), until: today() })
      expect(found.non_print_waste_g).toBe(30)
      expect(found.non_print_waste.filter((row) => row.dimension === 'material').map((row) => row.wasted_g)).toEqual([30])
      expect(found.non_print_waste.some((row) => row.dimension === 'type' && row.key === T.JADE && row.wasted_g === 30)).toBe(true)
      expect(found.waste.every((row) => row.wasted_g === 0)).toBe(true)
      expect((await getMetrics({ since: '2020-01-01', until: '2020-01-02' })).non_print_waste_g).toBe(0)
    })

    it('books nothing for respooled, a bulk archive, a plain PATCH or 0 g left', async () => {
      await archiveSpool(spoolId(1), { archived: true, reason: 'respooled' })
      await bulkArchiveSpools([spoolId(2)], true)
      await patchSpool(spoolId(3), { archived: true })
      await archiveSpool(spoolId(4), { archived: true, reason: 'finished', final_grams: 0 })
      expect(await waste()).toBe(0)
    })

    it('a replace move archives the box spool and books its grams with cause replaced', async () => {
      const { box } = seedHomeReturn()
      expect((await getSpool(spoolId(7))).location_id).toBe(box)
      await moveSpool(spoolId(1), box, { ifOccupied: 'replace', expectedOccupantId: spoolId(7) })
      expect((await getSpool(spoolId(7))).archived_at).not.toBeNull()
      expect((await events(spoolId(7)))[0]?.data).toMatchObject({ waste_g: 300, cause: 'replaced' })
      expect((await getSpool(spoolId(1))).location_id).toBe(box)
      expect(await waste()).toBe(300)
    })

    it('a replace into a place that is no dry box is the plain move', async () => {
      expect((await getSpool(spoolId(2))).location_id).toBe(L.DRY_BOX)
      await moveSpool(spoolId(1), L.DRY_BOX, { ifOccupied: 'replace', expectedOccupantId: spoolId(2) })
      expect((await getSpool(spoolId(2))).archived_at).toBeNull()
      expect(await waste()).toBe(0)
    })

    it('refuses a replace that names no expected occupant (expected_occupant_required)', async () => {
      const { box } = seedHomeReturn()
      // The type requires the field; the test sends the body an older client could still send.
      expect(await refusal(moveSpool(spoolId(1), box, { ifOccupied: 'replace' } as unknown as Parameters<typeof moveSpool>[2]))).toMatchObject({ status: 422, code: 'expected_occupant_required' })
      expect((await getSpool(spoolId(7))).archived_at).toBeNull()
      expect((await getSpool(spoolId(1))).location_id).toBe(L.SHELF_A)
    })

    it('the closing preview carries the waste the archive would book', async () => {
      expect((await closingPreview(spoolId(10), 'damaged', null, null)).closing.non_print_waste_g).toBe(250)
      expect((await closingPreview(spoolId(10), 'respooled', null, null)).closing.non_print_waste_g).toBeNull()
      expect((await closingPreview(spoolId(10), 'finished', null, 0)).closing.non_print_waste_g).toBeNull()
    })
  })

  describe('dry boxes and the home return (#7167)', () => {
    it('lists only the spools awaiting a home answer with awaiting_home', async () => {
      expect(await listSpools({ awaitingHome: true })).toEqual([])
      seedHomeReturn()
      expect((await listSpools({ awaitingHome: true })).map((one) => one.number)).toEqual([11])
      expect((await listSpools({ awaitingHome: false })).map((one) => one.number)).not.toContain(11)
    })

    it('refuses a full box with box_full naming its spool, and changes nothing', async () => {
      const { box } = seedHomeReturn()
      expect(await refusal(moveSpool(spoolId(1), box))).toMatchObject({ status: 409, code: 'box_full', occupant_id: spoolId(7) })
      expect((await getSpool(spoolId(1))).location_id).toBe(L.SHELF_A)
    })

    it('refuses a replace of another occupant than expected_occupant_id, and archives nothing', async () => {
      const { box } = seedHomeReturn()
      await moveSpool(spoolId(7), L.SHELF_A)
      await moveSpool(spoolId(9), box)
      expect(await refusal(moveSpool(spoolId(1), box, { ifOccupied: 'replace', expectedOccupantId: spoolId(7) }))).toMatchObject({ status: 409, code: 'box_full', occupant_id: spoolId(9) })
      expect((await getSpool(spoolId(9))).archived_at).toBeNull()
      await moveSpool(spoolId(1), box, { ifOccupied: 'replace', expectedOccupantId: spoolId(9) })
      expect((await getSpool(spoolId(9))).archived_at).not.toBeNull()
    })

    it('answers the prompt with answers, and checks it', async () => {
      const { box } = seedHomeReturn()
      expect(await refusal(moveSpool(spoolId(7), box, { answers: NOBODY }))).toMatchObject({ status: 404 })
      expect(await refusal(moveSpool(spoolId(7), L.SHELF_A, { answers: spoolId(11) }))).toMatchObject({ status: 422, code: 'not_its_home' })
      await moveSpool(spoolId(7), box, { answers: spoolId(11) })
      const unloaded = await getSpool(spoolId(11))
      expect(unloaded.home_asked_at).toBeNull()
      expect(unloaded.location_id).toBeNull()
      expect(await listSpools({ awaitingHome: true })).toEqual([])
    })

    it('a copy of a spool in a dry box leaves the box out of the draft', async () => {
      seedHomeReturn()
      expect((await copySpool(spoolId(7))).location_id).toBeNull()
      expect((await copySpool(spoolId(1))).location_id).toBe(L.SHELF_A)
    })

    it('a bulk create refuses a full box, and two spools into one empty box, and writes nothing', async () => {
      const { box } = seedHomeReturn()
      const body = { spool: { material_id: M.PLA, weight_locked: false }, quantity: 1, location_id: box, presets: [], k_values: [] }
      const before = (await listSpools()).length
      expect(await refusal(bulkCreateSpools(body, 'box-1'))).toMatchObject({ status: 409, code: 'box_full', occupant_id: spoolId(7) })
      await moveSpool(spoolId(7), L.SHELF_A)
      expect(await refusal(bulkCreateSpools({ ...body, quantity: 2 }, 'box-2'))).toMatchObject({ status: 409, code: 'box_full' })
      expect((await listSpools()).length).toBe(before)
      const [made] = await bulkCreateSpools(body, 'box-3')
      expect(made?.location_id).toBe(box)
    })

    /** S-7 archived in its box (a bulk archive keeps the place), and S-1 moved in after it. */
    async function boxTakenAfterArchive() {
      const { box } = seedHomeReturn()
      await bulkArchiveSpools([spoolId(7)], true)
      await moveSpool(spoolId(1), box)
    }

    it('a restore through the archive route into a taken box is box_full, and restores nothing', async () => {
      await boxTakenAfterArchive()
      expect(await refusal(archiveSpool(spoolId(7), { archived: false }))).toMatchObject({ status: 409, code: 'box_full', occupant_id: spoolId(1) })
      expect((await getSpool(spoolId(7))).archived_at).not.toBeNull()
    })

    it('a restore through PATCH into a taken box is box_full, and writes nothing', async () => {
      await boxTakenAfterArchive()
      expect(await refusal(patchSpool(spoolId(7), { archived: false, note: 'back' }))).toMatchObject({ status: 409, code: 'box_full', occupant_id: spoolId(1) })
      const row = await getSpool(spoolId(7))
      expect(row.archived_at).not.toBeNull()
      expect(row.note).not.toBe('back')
    })

    it('a bulk restore with one spool whose box is taken is box_full, and restores none', async () => {
      await boxTakenAfterArchive()
      await bulkArchiveSpools([spoolId(3)], true)
      expect(await refusal(bulkArchiveSpools([spoolId(3), spoolId(7)], false))).toMatchObject({ status: 409, code: 'box_full', occupant_id: spoolId(1) })
      expect((await getSpool(spoolId(3))).archived_at).not.toBeNull()
      expect((await getSpool(spoolId(7))).archived_at).not.toBeNull()
    })

    it('a bulk restore of two archived spools in one box restores neither: the first is the second one’s occupant', async () => {
      await boxTakenAfterArchive()
      await bulkArchiveSpools([spoolId(1)], true)
      expect(await refusal(bulkArchiveSpools([spoolId(7), spoolId(1)], false))).toMatchObject({ status: 409, code: 'box_full', occupant_id: spoolId(1) })
      expect((await getSpool(spoolId(1))).archived_at).not.toBeNull()
      expect((await getSpool(spoolId(7))).archived_at).not.toBeNull()
    })

    it('refuses to make a kind dry storage while one of its capacity-1 places holds several spools (box_has_many)', async () => {
      await upsertLocation({ capacity: 1 }, L.SHELF_A)
      expect(await refusal(upsertCatalog('location-kinds', { dry_storage: true }, LK.SHELF))).toMatchObject({ status: 422, code: 'box_has_many' })
      expect((await listCatalog('location-kinds')).find((one) => one.id === LK.SHELF)?.dry_storage).toBe(false)
    })

    it('refuses to make a box of a place that holds several spools (box_has_many)', async () => {
      expect(await refusal(upsertLocation({ capacity: 1 }, L.DRY_BOX))).toMatchObject({ status: 422, code: 'box_has_many' })
      expect((await listLocations()).flatMap((one) => one.children ?? []).find((one) => one.id === L.DRY_BOX)?.capacity).toBe(4)
    })
  })

  it('moves a spool, and refuses a place that does not exist', async () => {
    expect((await moveSpool(spoolId(9), L.SHELF_A)).location_id).toBe(L.SHELF_A)
    expect(await refusal(moveSpool(spoolId(9), NOBODY))).toEqual({
      status: 422,
      title: 'Unprocessable Content',
      detail: "the place to move to does not exist, is archived or is an AMS unit's place",
    })
  })

  it('adds a priced type with its price book row, and refuses a bad one', async () => {
    const body = { code: 'BL-PLA-BASIC-CYAN', material_id: M.PLA, brand_id: B.BAMBU, rgba: '#0086d680', label_weight_g: 1000, cost_per_kg: '18.5' } as SpoolTypeCreate
    const made = await upsertType(body)
    expect(made.filament_id).not.toBeNull()
    const book = await api.priceBook()
    expect(book.filaments.find((row) => row.id === made.filament_id)).toMatchObject({
      material: 'PLA',
      brand: 'Bambu Lab',
      colour: '#0086D6',
      alpha: 0x80,
      cost_per_kg: '18.5000',
    })
    expect(await refusal(upsertType(body))).toMatchObject({ status: 409, detail: 'a row of this catalog already has that name' })
    expect(await refusal(upsertType({ ...body, code: 'X', brand_id: M.PLA }))).toMatchObject({
      status: 422,
      detail: 'a row this one points at does not exist',
    })
    expect(await refusal(upsertType({ ...body, code: 'X', bed_temp_min: 80, bed_temp_max: 60 }))).toMatchObject({
      status: 422,
      detail: 'bed_temp_min is above bed_temp_max',
    })
    expect(await refusal(upsertType({ nozzle_temp_min: 300, nozzle_temp_max: 200 }, T.JADE))).toMatchObject({
      status: 422,
      detail: 'nozzle_temp_min is above nozzle_temp_max',
    })
  })

  it("compares a type's code in any case, as `filament_spool_types_code` does", async () => {
    const body = { code: 'PLA-BLK', material_id: M.PLA, rgba: '#000000', label_weight_g: 1000 } as SpoolTypeCreate
    await upsertType(body)
    expect(await refusal(upsertType({ ...body, code: 'pla-blk' }))).toMatchObject({ status: 409 })
  })

  it('refuses to restore a type or a place whose name a live one has taken since', async () => {
    const body = { code: 'PETG-RED', material_id: M.PETG, rgba: '#FF0000', label_weight_g: 1000 } as SpoolTypeCreate
    const old = await upsertType(body)
    await upsertType({ archived: true }, old.id)
    await upsertType(body)
    expect(await refusal(upsertType({ archived: false }, old.id))).toMatchObject({ status: 409 })

    const place = await upsertLocation({ name: 'Cupboard', kind_id: LK.SHELF, sort_order: 0 })
    await upsertLocation({ archived: true }, place.id)
    await upsertLocation({ name: 'cupboard', kind_id: LK.SHELF, sort_order: 0 })
    expect(await refusal(upsertLocation({ archived: false }, place.id))).toMatchObject({ status: 409 })
  })

  it("keeps a person's colour as it is when a sync finds it case-blind (#6907)", async () => {
    // A person's own Black PLA Basic, in other letter case than the sync names it.
    await upsertCatalog('colours', { name: 'black', manufacturer: 'bambu lab', material_name: 'pla basic', rgba: '#111111FF', sort_order: 0 })
    const run = await startColourSync('bambu')
    expect([run.added, run.kept, run.updated]).toEqual([1, 1, 0])
    const black = (await listCatalog('colours')).filter((row) => row.name.toLowerCase() === 'black')
    expect(black.map((row) => [row.rgba, row.source])).toEqual([['#111111FF', null]])
    // A second run finds its own row unchanged and still keeps the person's.
    const again = await startColourSync('bambu')
    expect([again.added, again.kept, again.unchanged]).toEqual([0, 1, 1])
  })

  it('keeps a colour unique per manufacturer, name and material, as `filament_colours_name` does', async () => {
    const black = { name: 'Black', rgba: '#000000FF', sort_order: 0 }
    expect(await upsertCatalog('colours', { ...black, manufacturer: 'Bambu Lab' })).toMatchObject({ name: 'Black' })
    // The same name of another material is another colour (#6907).
    expect(await upsertCatalog('colours', { ...black, manufacturer: 'Bambu Lab', material_name: 'PETG HF' })).toMatchObject({ material_name: 'PETG HF' })
    expect(await refusal(upsertCatalog('colours', { ...black, manufacturer: 'bambu lab', material_name: 'petg hf' }))).toMatchObject({ status: 409 })
    expect(await upsertCatalog('colours', { ...black, manufacturer: 'Polymaker' })).toMatchObject({ name: 'Black' })
    expect(await upsertCatalog('colours', black)).toMatchObject({ manufacturer: null })
    expect(await refusal(upsertCatalog('colours', { ...black, manufacturer: 'bambu lab' }))).toMatchObject({ status: 409 })
  })

  it('refuses to restore a catalog row whose name a live row has taken since', async () => {
    const old = await upsertCatalog('insert-materials', { name: 'Foam' })
    await upsertCatalog('insert-materials', { archived: true }, old.id)
    // An archived row holds no name: the index is `WHERE archived_at IS NULL`.
    await upsertCatalog('insert-materials', { name: 'FOAM' })
    expect(await upsertCatalog('insert-materials', { sort_order: 5 }, old.id)).toMatchObject({ sort_order: 5 })
    expect(await refusal(upsertCatalog('insert-materials', { archived: false }, old.id))).toMatchObject({ status: 409 })
  })

  it('keeps a plus variant apart, and reads + as PLUS (R-6706-17)', async () => {
    const plus = await upsertCatalog('materials', { name: 'PLA+' })
    expect(await refusal(upsertCatalog('materials', { name: 'PLA Plus' }))).toMatchObject({ status: 409, material_id: plus.id })
    expect(await resolveCatalog('materials', 'pla')).toMatchObject({ id: M.PLA })
  })

  it('keeps an archived row’s names and aliases in a catalog with aliases (#6706)', async () => {
    const old = await upsertCatalog('brands', { name: 'Hatchbox' })
    await setCatalogAliases('brands', old.id, ['HB'])
    await upsertCatalog('brands', { archived: true }, old.id)
    for (const name of ['hatch-box', 'h.b']) {
      expect(await refusal(upsertCatalog('brands', { name }))).toMatchObject({ status: 409, brand_id: old.id, brand_name: 'Hatchbox' })
    }
    expect(await resolveCatalog('brands', 'hb')).toMatchObject({ id: old.id })
  })

  it('refuses a catalog row whose name is taken, whose reference is missing, or that clears its name', async () => {
    expect(await refusal(upsertCatalog('brands', { name: 'bambu-lab' }))).toEqual({
      status: 409,
      title: 'Conflict',
      detail: "the brand 'Bambu Lab' already has a name or alias that matches 'bambu-lab'",
      brand_id: B.BAMBU,
      brand_name: 'Bambu Lab',
    })
    expect(await refusal(upsertCatalog('subtypes', { name: 'Glow', material_id: B.BAMBU }))).toMatchObject({
      status: 422,
      detail: 'a row this one points at does not exist',
    })
    expect(await refusal(upsertCatalog('insert-types', { name: 'Glass spool', empty_weight_g: 300, insert_material_id: IT.CARDBOARD }))).toMatchObject({
      status: 422,
    })
    expect(await refusal(upsertCatalog('materials', { name: 'ASA', nozzle_temp_min: 270, nozzle_temp_max: 240 }))).toMatchObject({
      status: 422,
      detail: 'nozzle_temp_min is above nozzle_temp_max',
    })
    expect(await refusal(upsertCatalog('brands', { name: null } as never, B.BAMBU))).toMatchObject({ status: 422, detail: 'name cannot be cleared' })
    expect(await upsertCatalog('brands', { name: 'Overture' })).toMatchObject({ name: 'Overture', archived_at: null })
  })

  it('refuses a place whose name is taken, whose kind is missing, or that would sit under itself', async () => {
    expect(await refusal(upsertLocation({ name: 'Shelf A', kind_id: LK.SHELF, parent_id: L.WORKSHOP, sort_order: 0 }))).toMatchObject({
      status: 409,
      detail: 'a place with that name, that tray or that AMS unit already exists',
    })
    expect(await refusal(upsertLocation({ name: 'Shelf B', kind_id: L.WORKSHOP, sort_order: 0 }))).toMatchObject({
      status: 422,
      detail: 'a row this one points at does not exist',
    })
    expect(await refusal(upsertLocation({ parent_id: L.SHELF_A }, L.WORKSHOP))).toMatchObject({
      status: 409,
      detail: 'a place cannot be its own parent or sit under one of its own',
    })
  })

  it("names a printer's trays freely, and keeps each tray to one place, as `filament_locations_*` do", async () => {
    const tray = (ams_id: number, tray_id: number) => ({ name: 'Tray 1', kind_id: LK.AMS, printer_id: H2C.printer_id, ams_id, tray_id, sort_order: 0 })
    expect(await upsertLocation(tray(1, 0))).toMatchObject({ name: 'Tray 1', ams_id: 1 })
    expect(await upsertLocation(tray(2, 0))).toMatchObject({ name: 'Tray 1', ams_id: 2 })
    expect(await refusal(upsertLocation({ ...tray(1, 0), name: 'Other' }))).toMatchObject({ status: 409 })
    // A plain place of that name at the top is still free: the name index skips trays.
    expect(await upsertLocation({ name: 'Tray 1', kind_id: LK.SHELF, sort_order: 0 })).toMatchObject({ printer_id: null })
  })
})

describe('the spools CSV import (Task 15)', () => {
  it('previews without writing: a new name is marked, a number updates only what differs', async () => {
    const preview = await importSpools(
      [
        { material: 'pla', brand: 'Acme', colour_name: 'Red' },
        { number: '3', material: 'PLA', note: 'Top shelf', location: 'shelf a' },
      ],
      { dryRun: true },
    )
    expect(preview.rows).toEqual([
      {
        line: 2,
        action: 'create',
        number: null,
        changes: { material: [null, 'PLA'], brand: [null, 'Acme', { new: true }], colour_name: [null, 'Red'] },
        error: null,
      },
      { line: 3, action: 'update', number: 3, changes: { note: [null, 'Top shelf'] }, error: null },
    ])
    expect(await listSpools()).toHaveLength(11)
  })

  it('writes once per key, answers a retry with the first result, and refuses the key for another body', async () => {
    const rows = [{ material: 'PLA', brand: 'Acme' }]
    const first = await importSpools(rows, { dryRun: false, key: 'k-1' })
    expect(first.rows[0]).toMatchObject({ action: 'create', number: 20 })
    expect(await importSpools(rows, { dryRun: false, key: 'k-1' })).toEqual(first)
    expect(await listSpools()).toHaveLength(12)
    expect(await refusal(importSpools([{ material: 'PETG' }], { dryRun: false, key: 'k-1' }))).toMatchObject({ status: 422 })
  })

  it('makes N spools once per key, and refuses a tray place or another body (#7150)', async () => {
    const body = { spool: { material_id: M.PLA, weight_locked: false }, quantity: 2, location_id: L.SHELF_A, presets: [], k_values: [] }
    const first = await bulkCreateSpools(body, 'b-1')
    expect(first.map((one) => one.number)).toEqual([20, 21])
    expect(first.every((one) => one.location_id === L.SHELF_A)).toBe(true)
    expect(await bulkCreateSpools(body, 'b-1')).toEqual(first)
    expect(await listSpools()).toHaveLength(13)
    expect(await refusal(bulkCreateSpools({ ...body, quantity: 3 }, 'b-1'))).toMatchObject({ status: 422 })
    expect(await refusal(bulkCreateSpools({ ...body, location_id: L.AMS_A1 }, 'b-2'))).toMatchObject({ status: 422 })
    expect(await listSpools()).toHaveLength(13)
  })

  it('edits spools all or none, and archives and restores them in one request (#7151)', async () => {
    expect(await bulkUpdateSpools([spoolId(1), spoolId(3), NOBODY], { note: 'June' })).toEqual({ changed: [spoolId(1), spoolId(3)].sort(), unchanged: [], not_found: [NOBODY] })
    expect((await getSpool(spoolId(3))).note).toBe('June')
    expect(await refusal(bulkUpdateSpools([spoolId(1)], { archived: true }))).toMatchObject({ status: 422 })
    expect(await refusal(bulkUpdateSpools([spoolId(1)], {}))).toMatchObject({ status: 422 })
    const archived = await bulkArchiveSpools([spoolId(1), NOBODY], true)
    expect(archived).toEqual({ changed: [spoolId(1)], unchanged: [], not_found: [NOBODY] })
    expect((await getSpool(spoolId(1))).archived_at).not.toBeNull()
    expect(await bulkArchiveSpools([spoolId(1)], true)).toEqual({ changed: [], unchanged: [spoolId(1)], not_found: [] })
    expect(await bulkArchiveSpools([spoolId(1)], false)).toEqual({ changed: [spoolId(1)], unchanged: [], not_found: [] })
  })

  it('takes the spool out of a tray (#7151)', async () => {
    await api.unlinkTraySpool(H2C.printer_id, 0, 0)
    expect((await getSpool(spoolId(4))).location_id).toBeNull()
  })

  it("copies a spool's own fields and not its tray, weight or lock (#7150)", async () => {
    const draft = await copySpool(spoolId(4))
    expect(draft.spool).toMatchObject({ type_id: T.BLACK, weight_locked: false, grams_left: null })
    expect(draft.location_id).toBeNull()
    expect((await copySpool(spoolId(6))).spool).toMatchObject({ material_id: M.PLA, colour_name: 'Teal', label_weight_g: 1000 })
    expect(await refusal(copySpool(NOBODY))).toMatchObject({ status: 404 })
  })

  it('makes a spool from a tray place once per key (#7150)', async () => {
    await moveSpool(spoolId(8), null)
    const made = await api.newTraySpool(H2C.printer_id, 0, 3, 's-1')
    expect(made.location_id).toBe(L.AMS_A4)
    expect(await api.newTraySpool(H2C.printer_id, 0, 3, 's-1')).toEqual(made)
    expect(await refusal(api.newTraySpool(H2C.printer_id, 1, 0, 's-2'))).toMatchObject({ status: 409 })
  })

  it('refuses a write with an error line, and writes none of its rows', async () => {
    const rows = [{ material: 'PLA', brand: 'Acme' }, { number: '999' }, { number: '1', location: 'Nowhere' }]
    expect(await refusal(importSpools(rows, { dryRun: false, key: 'k-2' }))).toEqual({
      status: 422,
      title: 'Unprocessable Content',
      detail: "line 3: no spool #999; line 4: no location named 'Nowhere'",
    })
    expect(await listSpools()).toHaveLength(11)
    expect((await upsertCatalog('brands', { name: 'Acme', sort_order: 0 })).name).toBe('Acme')
  })

  it('needs a key to write', async () => {
    const body = JSON.stringify({ rows: [{ material: 'PLA' }], dry_run: false })
    expect(await refusal(request('/filament/spools/import', { method: 'POST', body }))).toMatchObject({ status: 428 })
  })
})

describe('a new spool read under its label weight (#7683)', () => {
  it('the check names the shortfall and the three readings with their numbers', async () => {
    const check = await insertWeightOffer(spoolId(9), 900)
    expect(check).toMatchObject({ reason: 'below_label', offer: null, shortfall_g: 100, default: 'tared', filament_g: 720 })
    expect(check.resolutions).toEqual([
      { kind: 'tared', filament_g: 900, new_label_weight_g: null },
      { kind: 'short', filament_g: 720, new_label_weight_g: null },
      { kind: 'label_wrong', filament_g: 720, new_label_weight_g: 720 },
    ])
    expect((await insertWeightOffer(spoolId(9), 100)).resolutions.map((one) => one.kind)).toEqual(['tared'])
    expect((await insertWeightOffer(spoolId(9), 1000)).reason).toBe('not_above_label')
  })

  it('a weighing with a resolution books it and its side effect; one the check does not offer is a 422', async () => {
    const short = await addSpoolEvent(spoolId(9), { kind: 'weighed', source: 'scale', reading_g: 800, resolution: 'short' })
    expect(short.grams_after).toBe(620)
    expect(short.data).toMatchObject({ resolution: 'short', shortfall_g: 200, empty_source: 'insert', empty_g: 180 })
    expect((await getSpool(spoolId(9))).added_full).toBe(false)
    const kinds = (await listSpoolEvents(spoolId(9))).map((one) => one.kind)
    expect(kinds.slice(-2)).toEqual(['edited', 'weighed'])
    expect(await refusal(addSpoolEvent(spoolId(1), { kind: 'weighed', reading_g: 900, resolution: 'tared' }))).toMatchObject({
      status: 422,
      code: 'no_below_label',
      reason: 'used',
    })
  })

  it('a tared reading is the filament, and label_wrong sets the label', async () => {
    const tared = await addSpoolEvent(spoolId(9), { kind: 'weighed', source: 'scale', reading_g: 950, resolution: 'tared' })
    expect(tared.grams_after).toBe(950)
    expect(tared.data).toMatchObject({ resolution: 'tared', empty_source: 'tared', empty_g: 0, empty_basis: null, empty_name: null, expected_g: 1000, drift_g: null })
    expect((await getSpool(spoolId(9))).last_drift_g).toBeNull()
    await resetMockState()
    const fixed = await addSpoolEvent(spoolId(9), { kind: 'weighed', source: 'scale', reading_g: 900, resolution: 'label_wrong' })
    expect(fixed.grams_after).toBe(720)
    const spool = await getSpool(spoolId(9))
    expect(spool.label_weight_g).toBe(720)
    expect((await listSpoolEvents(spoolId(9))).at(-2)?.data).toEqual({ changes: { label_weight_g: [null, 720] } })
  })
})

describe('extra keys in a write body (every body model of `filament/models.py` forbids them)', () => {
  const body = (extra: object, base: object = {}) => JSON.stringify({ ...base, ...extra })
  const rows = (row: object) => JSON.stringify([row])
  const PRESET = { printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA00' }
  const K = { printer_id: H2C.printer_id, extruder: 0, nozzle_diameter: '0.4', k_value: '0.02' }
  const cases: [string, string, string, string][] = [
    ['PUT spool presets', 'PUT', `/filament/spools/${spoolId(1)}/presets`, rows({ ...PRESET, migrated: true })],
    ['PUT type presets', 'PUT', `/filament/types/${T.JADE}/presets`, rows({ ...PRESET, overridden: false })],
    ['PUT spool K values', 'PUT', `/filament/spools/${spoolId(1)}/k-values`, rows({ ...K, overridden: false })],
    ['PUT type K values', 'PUT', `/filament/types/${T.JADE}/k-values`, rows({ ...K, type_id: T.JADE })],
    ['PATCH a spool', 'PATCH', `/filament/spools/${spoolId(1)}`, body({ bogus: 1 })],
    ['POST a spool', 'POST', '/filament/spools', body({ bogus: 1 }, { type_id: T.JADE })],
    ['PATCH a type', 'PATCH', `/filament/types/${T.JADE}`, body({ bogus: 1 })],
    ['POST a type', 'POST', '/filament/types', body({ bogus: 1 }, { code: 'X', material_id: M.PLA, rgba: '#FFFFFFFF', diameter_mm: '1.75', label_weight_g: 1000 })],
    ['POST a move', 'POST', `/filament/spools/${spoolId(1)}/move`, body({ bogus: 1 }, { location_id: null })],
    ['POST an archive', 'POST', `/filament/spools/${spoolId(1)}/archive`, body({ bogus: 1 })],
    ['POST a tag', 'POST', `/filament/spools/${spoolId(1)}/tags`, body({ bogus: 1 }, { tag_uid: '04AABB' })],
    ['POST an event', 'POST', `/filament/spools/${spoolId(1)}/events`, body({ bogus: 1 }, { kind: 'weighed', grams: 5 })],
    ['POST a location', 'POST', '/filament/locations', body({ bogus: 1 }, { name: 'X', kind_id: LK.SHELF })],
    ['POST a catalog row', 'POST', '/filament/catalog/brands', body({ bogus: 1 }, { name: 'Nope' })],
    ['PATCH a catalog row', 'PATCH', `/filament/catalog/materials/${M.PLA}`, body({ bogus: 1 })],
    ['PATCH a type archive', 'PATCH', `/filament/types/${T.JADE}`, body({ bogus: 1 }, { archived: true })],
    ['POST a CSV import', 'POST', '/filament/spools/import', body({ bogus: 1 }, { rows: [{ material: 'PLA' }], dry_run: true })],
    ['POST a CSV import row', 'POST', '/filament/spools/import', body({ rows: [{ bogus: 'x' }], dry_run: true })],
    ['PATCH a location', 'PATCH', `/filament/locations/${L.SHELF_A}`, body({ bogus: 1 })],
  ]
  it.each(cases)('refuses it on %s', async (_name, method, path, payload) => {
    expect(await refusal(request(path, { method, body: payload }))).toMatchObject({ status: 422, detail: expect.stringContaining('Extra inputs are not permitted') })
  })
})

describe('the sensors list (#6684)', () => {
  it('leaves out the sensors of an archived place, as the backend does', async () => {
    resetMockState()
    expect((await listSensors()).map((one) => one.id)).toContain(SENSOR.BOX_HUMIDITY)
    expect((await listSensors(L.DRY_BOX)).map((one) => one.id)).toContain(SENSOR.BOX_HUMIDITY)
    await upsertLocation({ archived: true }, L.DRY_BOX)
    const ids = (await listSensors()).map((one) => one.id)
    expect(ids).not.toContain(SENSOR.BOX_HUMIDITY)
    expect(ids).not.toContain(SENSOR.BOX_TEMP)
    expect(ids).toContain(SENSOR.SHELF_HUMIDITY)
    expect(await listSensors(L.DRY_BOX)).toEqual([])
  })
})

describe('the saved label sheets (#7216)', () => {
  const layout = { page: 'letter' as const, width_mm: 60, height_mm: 30, cols: 2, rows: 5, top_mm: 10, left_mm: 5, col_gap_mm: 2, row_gap_mm: 1 }

  it('refuses a name that matches another by case folding, as `name_key` does', async () => {
    resetMockState()
    await createLabelSheet({ name: 'Straße', layout })
    await expect(createLabelSheet({ name: 'STRASSE', layout })).rejects.toMatchObject({ status: 409 })
    await expect(createLabelSheet({ name: 'ΣΟΦΟΣ', layout })).resolves.toMatchObject({ name: 'ΣΟΦΟΣ' })
    // A final sigma (ς) and a plain one (σ) fold the same.
    await expect(createLabelSheet({ name: 'σοφοσ', layout })).rejects.toMatchObject({ status: 409 })
  })
})
