import { HttpResponse, http } from 'msw'
import type {
  PrintCheck,
  PrinterRackAlgorithm,
  PrinterRackUsage,
  RackHotendUsage,
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

/** The mock H2C's rack side: three eligible 0.4 mm Standard hotends, no serials (§7: never in /check). */
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

const DAY = 24 * 3600 * 1000

/** What each mock position's hotend reports and ran (#2170). Serials are invented. */
const HOTENDS: Record<number, Partial<RackHotendUsage>> = {
  2: {
    serial: 'TEST-HOTEND-17',
    wear: 12,
    filament_id: 'GFA00',
    filament_name: 'Bambu PLA Basic',
    filament_material: 'PLA',
    filament_colour: '#3F8E43',
    spools: [
      { spool_id: 7, label: 'Bambu PLA Basic Mistletoe Green', material: 'PLA', colour: '#3F8E43', prints: 9, grams: 412.4, last_used_at: null },
      { spool_id: 8, label: 'Inland PLA Black', material: 'PLA', colour: '#000000', prints: 5, grams: 180, last_used_at: null },
    ],
  },
  4: { serial: null, wear: 0, prints: 0, print_seconds: 0, last_used_at: null, filament_id: null, filament_name: null, filament_material: null, filament_colour: null, spools: [] },
  6: {
    serial: 'TEST-HOTEND-21',
    wear: 128,
    filament_id: 'GFG99',
    filament_name: 'Generic PETG',
    filament_material: 'PETG',
    filament_colour: '#1E90FF',
    spools: [{ spool_id: 12, label: 'Overture PETG Blue', material: 'PETG', colour: '#1E90FF', prints: 9, grams: 655, last_used_at: null }],
  },
}

function mockHotend(option: RackOption): RackHotendUsage {
  const extra = HOTENDS[option.position] ?? {}
  const used = option.prints > 0
  return {
    position: option.position,
    nozzle_diameter: option.nozzle_diameter,
    nozzle_type: option.nozzle_type,
    high_flow: option.flow === 'high_flow',
    prints: option.prints,
    print_seconds: option.print_seconds,
    grams: (extra.spools ?? []).reduce((sum, spool) => sum + spool.grams, 0),
    pending: option.pending,
    first_seen_at: new Date(Date.now() - 30 * DAY).toISOString(),
    last_used_at: used ? new Date(Date.now() - option.position * DAY).toISOString() : null,
    ...extra,
  }
}

export const handlers = [
  // #1298, #2170 — printer 1's rack: the counts the check's options show, each hotend's
  // invented serial, wear and loaded filament as the H2C reports them (position 4's mount
  // says "N/A", which the backend answers as no serial), and the spools each one ran.
  // Printer 2 answers no hotends, as a printer with no rack does.
  http.get(`${base}/print/printers/:id/rack-usage`, ({ params }) =>
    HttpResponse.json({
      printer_id: Number(params['id']),
      hotends: Number(params['id']) === 1 ? OPTIONS.map(mockHotend) : [],
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
