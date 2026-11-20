import type { PrintRunRequest, TowerCheck } from '../api/types'

/**
 * #6758 — the check's prime tower (`backend/scadbuddy/bambuddy/tower_check.py`), for the
 * mock printers: with "No sparse layers" chosen, plate 1's tower stands inside the keep-out
 * until the request moves it (`wipe_tower_x`/`_y` in `process_overrides`). The free spot is
 * the one the backend finds for a 30 mm object in the middle of an H2C.
 */
export function mockTowerCheck(body: PrintRunRequest): TowerCheck {
  const asked = body.prime_tower?.enabled === true && body.prime_tower.no_sparse_layers === true
  if (!asked || body.process_overrides?.['wipe_tower_x'] !== undefined) return { conflicts: [], fix: null }
  return {
    conflicts: [
      {
        plate_id: 1,
        reason: 'keep-out',
        x: 200,
        y: 150,
        move_x: 157.243,
        move_y: 5,
        message: 'Plate 1: the prime tower is closer to the objects than the 100 mm "No sparse layers" needs. Move the prime tower to a free spot.',
      },
    ],
    fix: { wipe_tower_x: ['157.243'], wipe_tower_y: ['5'] },
  }
}
