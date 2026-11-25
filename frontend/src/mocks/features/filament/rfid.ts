import { HttpResponse, http } from 'msw'
import type { components } from '../../../api/schema'
import { mockSettings } from '../../handlers'

type S = components['schemas']

/**
 * #6264 — `/api/v1/filament/rfid/*` (`backend/scadbuddy/api/filament.py`): the RFID override
 * mode and the reconcile. This module owns those routes (one route, one feature module); the
 * spool, tag and binding routes stay in `index.ts`. Its answers are the happy path. A test
 * serves the other answers with `server.use`.
 */

const BASE = '/api/v1/filament'

let overrideMode = false

export function reset() {
  overrideMode = false
}

/** #7156 — the mode and the setting `filament_auto_add_unknown_rfid` are two holds on one value. */
const status = (): S['RfidStatus'] => {
  const autoAdd = mockSettings().filament_auto_add_unknown_rfid !== false
  const holds = overrideMode || !autoAdd
  return {
    override_mode: overrideMode,
    auto_add_unknown_rfid: autoAdd,
    state: holds ? 'applied' : 'off',
    bambuddy_auto_add: holds ? false : null,
    saved_auto_add: holds ? true : null,
    detail: null,
  }
}

export const handlers = [
  http.get(`${BASE}/rfid/status`, () => HttpResponse.json(status())),
  http.put(`${BASE}/rfid/override-mode`, async ({ request }) => {
    overrideMode = ((await request.json()) as S['RfidModeSet']).enabled
    return HttpResponse.json(status())
  }),
  http.post(`${BASE}/rfid/reconcile`, () =>
    HttpResponse.json({
      mode: status(),
      tags: { pushed: 0, moved: 0, cleared: 0, unchanged: 0, failed: 0 },
      slots: { configured: 0, unconfirmed: 0, waiting: 0, unchanged: 0, no_preset: 0, no_temps: 0, invalid_temps: 0, no_nozzle: 0, unsupported_unit: 0, failed: 0 },
    } satisfies S['RfidReconcileReport']),
  ),
]
