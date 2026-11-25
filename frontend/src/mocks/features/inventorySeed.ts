/**
 * #7067 — the ids of the inventory mock's seeded rows (`inventory.ts`), in a module that
 * imports nothing, so the e2e specs can name them without loading the mock API.
 * Every `.ts` under `features/` is loaded as a mock feature and must export `handlers`
 * (`mocks/features.ts`); this one serves none.
 */
export const handlers = []

export const INVENTORY = {
  HOOK: 'ab700000-0000-4000-8000-000000000001',
  NOZZLE: 'ab700000-0000-4000-8000-000000000002',
  ENGRAVING: 'ab700000-0000-4000-8000-000000000003',
  HOOK_LOT: 'ab720000-0000-4000-8000-000000000001',
  NOZZLE_UNIT: 'ab710000-0000-4000-8000-000000000001',
  HOOK_OFFER: 'ab730000-0000-4000-8000-000000000001',
}
