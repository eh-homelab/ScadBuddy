import { beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { inventoryApi } from '../../pages/inventory/api'
import { resolveCode } from '../../api/filament'
import { resetMockState } from '../handlers'
import { INVENTORY, seedItem, seedLot, seedOffer, takeStock } from './inventory'
import { SUPPLIER } from './filamentPurchasingSeed'
import { seedEntityProperties } from './entities'

const refusal = async (promise: Promise<unknown>) => {
  const error = await promise.then(
    () => null,
    (caught: unknown) => caught,
  )
  expect(error).toBeInstanceOf(ApiError)
  const api = error as ApiError
  return { status: api.status, type: api.problem.type }
}
const INVENTORY_PROBLEM = 'https://scadbuddy.dev/problems/inventory/'

const NO_PLACE = 'ab7e0000-0000-4000-8000-000000000001'

/** #7067 review — the inventory mock answers as the backend does. */
describe('inventory mock', () => {
  beforeEach(() => resetMockState())

  it('refuses stock calls on a service as Go does: receive and count are step_not_applicable, use handler_not_built (#6949 K1)', async () => {
    const service = seedItem({ name: 'Laser cutting', kind_id: 'service' })
    const refused = (code: string) => ({ status: 409, type: `${INVENTORY_PROBLEM}${code}` })
    expect(await refusal(inventoryApi.receiveLot(service.id, { qty: '1' }))).toEqual(refused('step_not_applicable'))
    expect(await refusal(inventoryApi.count(service.id, { on_hand: '1', note: 'shelf' }))).toEqual(refused('step_not_applicable'))
    expect(await refusal(inventoryApi.use(service.id, { qty: '1', reason: '' }))).toEqual(refused('handler_not_built'))
  })

  it('refuses an item without its required properties as Go does, and marks a read (R-K1-33)', async () => {
    await inventoryApi.createKind({ id: 'strict', name: 'Strict', parent_id: 'hardware', attribute_schema: { type: 'object', properties: { material: { type: 'string' } }, required: ['material'] } })
    const schema = await inventoryApi.kindSchema('strict')
    expect(schema.item_required).toEqual(['material'])
    const missing = await inventoryApi.createItem({ kind_id: 'strict', name: 'Bolt' }).catch((e: unknown) => e as ApiError)
    expect(missing).toBeInstanceOf(ApiError)
    expect((missing as ApiError).problem).toMatchObject({ code: 'missing_required_properties', missing: ['material'], invalid: [] })
    const bad = await inventoryApi.createItem({ kind_id: 'strict', name: 'Bolt', properties: { material: 7 } }).catch((e: unknown) => e as ApiError)
    expect((bad as ApiError).problem.code).toBe('bad_property_value')
    expect((await inventoryApi.items({ kind: 'strict' })).length).toBe(0)

    const made = await inventoryApi.createItem({ kind_id: 'strict', name: 'Bolt', properties: { material: 'steel' } })
    expect(made.missing_properties).toEqual([])
    // An empty patch is not checked; a patch that empties the key is refused.
    await inventoryApi.patchItem(made.id, {})
    const emptied = await inventoryApi.patchItem(made.id, { properties: { material: null } }).catch((e: unknown) => e as ApiError)
    expect((emptied as ApiError).problem.code).toBe('missing_required_properties')

    const old = seedItem({ name: 'Old bolt', kind_id: 'strict' })
    expect((await inventoryApi.item(old.id)).missing_properties).toEqual(['material'])
    await inventoryApi.archiveItem(old.id)
    expect((await inventoryApi.item(old.id)).missing_properties).toEqual([])
  })

  it('treats a patch of only `properties: null` as empty, as the forwarder drops it (R-K1-33)', async () => {
    await inventoryApi.createKind({ id: 'strict', name: 'Strict', parent_id: 'hardware', attribute_schema: { type: 'object', properties: { material: { type: 'string' } }, required: ['material'] } })
    const old = seedItem({ name: 'Old bolt', kind_id: 'strict' })
    expect((await inventoryApi.patchItem(old.id, { properties: null })).missing_properties).toEqual(['material'])
  })

  it('takes a required key a parent kind defines, as Go does (R-K1-33)', async () => {
    await inventoryApi.createKind({ id: 'base', name: 'Base', parent_id: 'hardware', attribute_schema: { type: 'object', properties: { material: { type: 'string' } } } })
    await inventoryApi.createKind({ id: 'strict', name: 'Strict', parent_id: 'base', attribute_schema: { type: 'object', required: ['material'] } })
    expect((await inventoryApi.kindSchema('strict')).item_required).toEqual(['material'])
    await inventoryApi.patchKind('strict', { attribute_schema: { type: 'object', required: ['material'] } })
    const unknown = await inventoryApi.createKind({ id: 'loose', name: 'Loose', parent_id: 'base', attribute_schema: { type: 'object', required: ['colour'] } }).catch((e: unknown) => e as ApiError)
    expect((unknown as ApiError).problem.code).toBe('bad_property_schema')
  })

  it('counts each key the items of a kind and of its descendants hold, archived ones too, in keys_in_use', async () => {
    await inventoryApi.createKind({ id: 'base', name: 'Base', parent_id: 'hardware' })
    await inventoryApi.createKind({ id: 'leaf', name: 'Leaf', parent_id: 'base' })
    const one = seedItem({ name: 'A', kind_id: 'base' })
    const two = seedItem({ name: 'B', kind_id: 'leaf', archived_at: '2026-11-01T00:00:00Z' })
    seedEntityProperties(`item:${one.id}`, { material: 'steel', grade: '8.8' })
    seedEntityProperties(`item:${two.id}`, { material: '' })
    expect((await inventoryApi.kindSchema('base')).keys_in_use).toEqual([
      { key: 'grade', count: 1 },
      { key: 'material', count: 2 },
    ])
    expect((await inventoryApi.kindSchema('leaf')).keys_in_use).toEqual([{ key: 'material', count: 1 }])
  })

  it('names a printer special type\'s model_code among the keys an item must have', async () => {
    await inventoryApi.createKind({ id: 'fdm', name: 'FDM printer', parent_id: 'printer', special_type: 'printer' })
    expect((await inventoryApi.kindSchema('fdm')).item_required).toEqual(['model_code'])
    const refused = await inventoryApi.createItem({ kind_id: 'fdm', name: 'H2C' }).catch((e: unknown) => e as ApiError)
    expect((refused as ApiError).problem).toMatchObject({ code: 'missing_required_properties', missing: ['model_code'] })
    expect((await inventoryApi.createItem({ kind_id: 'fdm', name: 'H2C', properties: { model_code: 'O1C' } })).missing_properties).toEqual([])
  })

  it('numbers every kind, makes a kind with no id k<number>, and resolves K-<number> (the owner, 2026-11-27)', async () => {
    const kinds = await inventoryApi.kinds()
    expect(new Set(kinds.map((k) => k.number)).size).toBe(kinds.length)
    expect(kinds.every((k) => k.code === `K-${k.number}`)).toBe(true)
    const made = await inventoryApi.createKind({ name: 'Magnet', parent_id: 'hardware' })
    expect(made.id).toBe(`k${made.number}`)
    expect(made.number).toBe(Math.max(...kinds.map((k) => k.number)) + 1)
    expect(await resolveCode(made.code)).toEqual({ kind: 'item_kind', id: made.id, code: made.code, name: 'Magnet', archived: false })
    const typed = await inventoryApi.createKind({ id: 'k12', name: 'X' }).catch((e: unknown) => e as ApiError)
    expect((typed as ApiError).status).toBe(422)
  })

  it('answers offer decimals normalized, as `_Decimals` writes them (R-P2-40)', async () => {
    const offer = await inventoryApi.createOffer({ supplier_id: SUPPLIER.AMAZON, item_id: INVENTORY.HOOK, sku: 'B0HOOK500', pack_size: '500.000000' })
    expect(offer.pack_size).toBe('500')
    const price = await inventoryApi.addPrice(offer.id, { price: '4.990' })
    expect(price.price).toBe('4.99')
  })

  it('keeps money exact: no float sum drifts (0.1 + 0.2 is 0.3)', async () => {
    const ring = seedItem({ kind_id: 'hardware', name: 'Split ring' })
    seedLot(ring.id, 1, '0.1')
    seedLot(ring.id, 1, '0.2')
    expect((await inventoryApi.item(ring.id)).stock_value_minor).toBe('0.3')
    const moves = takeStock(ring.id, 2)
    expect(moves.map((m) => m.amount_minor)).toEqual(['-0.1', '-0.2'])
  })

  it('refuses a place that does not exist with 404 `location_not_found`', async () => {
    const lot = await refusal(inventoryApi.receiveLot(INVENTORY.HOOK, { qty: '1', location_id: NO_PLACE }))
    expect(lot).toEqual({ status: 404, type: `${INVENTORY_PROBLEM}location_not_found` })
    const patch = await refusal(inventoryApi.patchLot(INVENTORY.HOOK_LOT, { location_id: NO_PLACE }))
    expect(patch.type).toBe(`${INVENTORY_PROBLEM}location_not_found`)
    const unit = await refusal(inventoryApi.patchUnit(INVENTORY.NOZZLE_UNIT, { location_id: NO_PLACE }))
    expect(unit.type).toBe(`${INVENTORY_PROBLEM}location_not_found`)
  })

  it("refuses a serial another unit of the item has with 409 `serial_exists`", async () => {
    const received = await refusal(inventoryApi.receiveUnits(INVENTORY.NOZZLE, { count: 2, serials: ['HS-0009', 'HS-0009'] }))
    expect(received).toEqual({ status: 409, type: `${INVENTORY_PROBLEM}serial_exists` })
    const renamed = await refusal(inventoryApi.patchUnit(INVENTORY.NOZZLE_UNIT, { serial: 'HS-0002' }))
    expect(renamed.type).toBe(`${INVENTORY_PROBLEM}serial_exists`)
    expect((await inventoryApi.patchUnit(INVENTORY.NOZZLE_UNIT, { serial: 'HS-0001' })).serial).toBe('HS-0001')
  })

  it("answers FastAPI's own 422 for a bad kind id and a blank count note, with no inventory code", async () => {
    const kind = await refusal(inventoryApi.createKind({ id: 'Bad Id', name: 'Bad', tracking: 'lot' }))
    expect(kind).toEqual({ status: 422, type: 'about:blank' })
    const count = await refusal(inventoryApi.count(INVENTORY.HOOK, { on_hand: '1', note: '  ' }))
    expect(count).toEqual({ status: 422, type: 'about:blank' })
  })

  it('normalizes a seeded offer pack too', () => {
    expect(seedOffer({ item_id: INVENTORY.HOOK, supplier_id: SUPPLIER.AMAZON, sku: 'X', pack_size: '10.50' }).pack_size).toBe('10.5')
  })
})
