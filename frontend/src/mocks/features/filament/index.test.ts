import { describe, expect, it } from 'vitest'
import { ApiError, api, request } from '../../../api/client'
import {
  archiveSpool,
  bindTag,
  createSpool,
  deleteSpool,
  getInventory,
  getSpool,
  getSummary,
  getTag,
  getType,
  importSpools,
  listEvents,
  listLocations,
  listSpools,
  listTags,
  moveSpool,
  patchSpool,
  unbindTag,
  upsertCatalog,
  upsertLocation,
  upsertType,
  type SpoolCreate,
  type SpoolPatch,
  type SpoolTypeCreate,
} from '../../../api/filament'
import { resetMockState } from '../../handlers'
import { B, H2C, IT, L, LK, M, spoolId, T, TAG } from './seed'

// #6262 Task 14 — the Filament section's mock API over its seed, through the client.

/** The problem a call was refused with: its status, title and detail. */
async function refusal(call: Promise<unknown>) {
  const error = await call.then(
    () => null,
    (cause: unknown) => cause,
  )
  expect(error).toBeInstanceOf(ApiError)
  const { status, title, detail } = (error as ApiError).problem
  return { status, title, detail }
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

  it('moves a spool, and refuses a place that does not exist', async () => {
    expect((await moveSpool(spoolId(9), L.SHELF_A)).location_id).toBe(L.SHELF_A)
    expect(await refusal(moveSpool(spoolId(9), NOBODY))).toEqual({
      status: 422,
      title: 'Unprocessable Content',
      detail: 'the place to move to does not exist or is archived',
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

  it('keeps a colour name unique per manufacturer, as `filament_colours_name` does', async () => {
    const black = { name: 'Black', rgba: '#000000FF', sort_order: 0 }
    expect(await upsertCatalog('colours', { ...black, manufacturer: 'Bambu Lab' })).toMatchObject({ name: 'Black' })
    expect(await upsertCatalog('colours', { ...black, manufacturer: 'Polymaker' })).toMatchObject({ name: 'Black' })
    expect(await upsertCatalog('colours', black)).toMatchObject({ manufacturer: null })
    expect(await refusal(upsertCatalog('colours', { ...black, manufacturer: 'bambu lab' }))).toMatchObject({ status: 409 })
  })

  it('refuses to restore a catalog row whose name a live row has taken since', async () => {
    const old = await upsertCatalog('brands', { name: 'Hatchbox' })
    await upsertCatalog('brands', { archived: true }, old.id)
    // An archived row holds no name: the index is `WHERE archived_at IS NULL`.
    await upsertCatalog('brands', { name: 'HATCHBOX' })
    expect(await upsertCatalog('brands', { sort_order: 5 }, old.id)).toMatchObject({ sort_order: 5 })
    expect(await refusal(upsertCatalog('brands', { archived: false }, old.id))).toMatchObject({ status: 409 })
  })

  it('refuses a catalog row whose name is taken, whose reference is missing, or that clears its name', async () => {
    expect(await refusal(upsertCatalog('brands', { name: 'bambu lab' }))).toEqual({
      status: 409,
      title: 'Conflict',
      detail: 'a row of this catalog already has that name',
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
      detail: 'a place with that name or that tray already exists',
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
