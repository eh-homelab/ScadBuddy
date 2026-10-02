import { HttpResponse, http } from 'msw'
import type { PrinterRackAlgorithm, RackAlgorithm } from '../../api/types'

/** #836 — the rack algorithm remembered per printer. */
const base = '/api/v1'
const state: { algorithms: Record<string, RackAlgorithm> } = { algorithms: {} }

export function reset(): void {
  state.algorithms = {}
}

export const handlers = [
  http.put(`${base}/print/printers/:id/rack-algorithm`, async ({ params, request }) => {
    const id = String(params['id'])
    const body = (await request.json()) as { algorithm: RackAlgorithm | null }
    if (body.algorithm === null) delete state.algorithms[id]
    else state.algorithms[id] = body.algorithm
    return HttpResponse.json({
      printer_id: Number(id),
      algorithm: state.algorithms[id] ?? 'least_used',
    } satisfies PrinterRackAlgorithm)
  }),
]
