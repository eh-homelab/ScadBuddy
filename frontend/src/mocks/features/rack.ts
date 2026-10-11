import { HttpResponse, http } from 'msw'
import type {
  PrintCheck,
  PrinterRackAlgorithm,
  PrinterRackUsage,
  RackAlgorithm,
  RackOption,
  RackSentPick,
} from '../../api/types'

/** #836 — the rack algorithm remembered per printer. */
const base = '/api/v1'
const state: { algorithms: Record<string, RackAlgorithm> } = { algorithms: {} }

export function reset(): void {
  state.algorithms = {}
}

/** The mock H2C's rack side: three eligible 0.4 mm Standard hotends, no serials (§7). */
const OPTIONS: RackOption[] = [
  { position: 2, nozzle_type: 'HS01', nozzle_diameter: '0.4', flow: 'standard', material: 'PLA', filament_type: 'PLA', color: '#FFFFFF', color_word: 'white', prints: 14, print_seconds: 151200, pending: 0 },
  { position: 4, nozzle_type: 'HS01', nozzle_diameter: '0.4', flow: 'standard', material: null, color: null, prints: 3, print_seconds: 25200, pending: 0 },
  { position: 6, nozzle_type: 'HS01', nozzle_diameter: '0.4', flow: 'standard', material: 'PETG', filament_type: 'PETG', color: '#1E90FF', color_word: 'blue', prints: 9, print_seconds: 86400, pending: 0 },
]

/** The check's rack preview (#836): least used unless a position is picked by hand. */
export function mockRackCheck(manual: number | null): PrintCheck {
  const picked = manual === null ? undefined : OPTIONS.find((o) => o.position === manual)
  const errors =
    manual !== null && picked === undefined
      ? [`Rack position ${manual} holds no hotend, and this prints with a 0.4 mm Standard nozzle. Choose another position, or Automatic.`]
      : []
  const position = picked?.position ?? 4
  return {
    errors,
    warnings: [],
    rack: {
      group_id: null,
      position,
      reason: picked ? 'chosen by hand' : 'least used: 3 prints',
      unsafe_material: false,
      glow_unchecked: false,
      options: OPTIONS,
    },
  }
}

/** The picks a run sent: one per plate, at the previewed position. */
export function mockRackPicks(manual: number | null, plates: number[]): RackSentPick[] {
  const position = OPTIONS.some((o) => o.position === manual) ? (manual as number) : 4
  return plates.map((plate_id) => ({ plate_id, group_id: 0, position }))
}

export const handlers = [
  // #1298 — the mock rack's hotends, with the counts the check's options show.
  http.get(`${base}/print/printers/:id/rack-usage`, ({ params }) =>
    HttpResponse.json({
      printer_id: Number(params['id']),
      hotends: OPTIONS.map((option) => ({
        position: option.position,
        nozzle_diameter: option.nozzle_diameter,
        nozzle_type: option.nozzle_type,
        high_flow: option.flow === 'high_flow',
        prints: option.prints,
        print_seconds: option.print_seconds,
        grams: 0,
        pending: option.pending,
        first_seen_at: null,
        last_used_at: null,
      })),
    } satisfies PrinterRackUsage),
  ),
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
