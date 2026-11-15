import { HttpResponse, http } from 'msw'
import type { ArchiveStats, QueueItem } from '../../api/types'
import { outputs } from '../fixtures'

/**
 * #2351 — Bambuddy's queue and the farm's totals: three waiting items (one for a
 * printer that ScadBuddy queued, #3741; one for any H2C, short of filament; one with no
 * picture, #3742) and finished history, which /prints never asks for (it reads pending only).
 */
export const PRINT_QUEUE: QueueItem[] = [
  {
    id: 257,
    printer_id: 1,
    printer_name: '3DP-31B-598',
    archive_name: 'Carrot Garden',
    status: 'printing',
    position: 6,
    print_time_seconds: 16716,
    filament_used_grams: 114.2,
    manual_start: false,
    filament_short: false,
  },
  {
    id: 259,
    printer_id: 1,
    printer_name: '3DP-31B-598',
    archive_name: 'Luna keychain',
    status: 'pending',
    position: 7,
    print_time_seconds: 2700,
    filament_used_grams: 12.4,
    manual_start: true,
    filament_short: false,
    // #3741 — a ScadBuddy run queued it.
    scadbuddy: {
      subject: 'output:luna-0002',
      output_id: 'luna-0002',
      library_file_id: null,
      slug: 'keychain',
      run_id: 'run-luna-2',
      archive_id: null,
      matched_by: 'queue_item',
    },
    // #3742 — ScadBuddy's own picture of its job.
    picture: '/api/v1/outputs/luna-0002/thumbnail',
  },
  {
    id: 260,
    printer_id: null,
    target_model: 'H2C',
    library_file_name: 'Bracket.gcode.3mf',
    status: 'pending',
    position: 8,
    waiting_reason: 'No active H2C printers in Workshop',
    scheduled_time: '2026-10-14T09:00:00Z',
    print_time_seconds: 5400,
    filament_used_grams: 48,
    manual_start: false,
    filament_short: true,
    // #3742 — Bambuddy's thumbnail of its library file, through ScadBuddy's farm proxy.
    picture: '/api/v1/farm/library/1615/thumbnail',
  },
  {
    id: 263,
    printer_id: null,
    target_model: 'H2C',
    library_file_name: 'Spool hook.3mf',
    status: 'pending',
    position: 9,
    print_time_seconds: 1800,
    filament_used_grams: 9,
    manual_start: false,
    filament_short: false,
    // #3742 — no picture anywhere: the row shows a placeholder.
    picture: null,
  },
  {
    id: 205,
    printer_id: 1,
    printer_name: '3DP-31B-598',
    archive_name: 'Old print',
    archive_id: 107,
    status: 'completed',
    manual_start: false,
    filament_short: false,
  },
]

/**
 * #3744 — items only the item page reads (`GET /farm/queue/{id}`), beside the queue's
 * own: a ScadBuddy job still waiting, with its output, trays and plate; and a ScadBuddy
 * job already printed and linked, whose page goes on to `/prints/35`. Any other id is an
 * item Bambuddy no longer has.
 */
export const QUEUE_ITEM_ONLY: QueueItem[] = [
  {
    id: 265,
    printer_id: 1,
    printer_name: '3DP-31B-598',
    library_file_id: 1640,
    library_file_name: 'Reagan.3mf',
    status: 'pending',
    position: 10,
    plate_id: 1,
    bed_type: 'Textured PEI Plate',
    ams_mapping: [0, 5],
    scheduled_time: '2026-10-14T09:00:00Z',
    print_time_seconds: 3600,
    filament_used_grams: 21.5,
    filament_type: 'PLA',
    filament_color: '#1B6CA8',
    manual_start: true,
    filament_short: true,
    waiting_reason: 'Waiting for the plate to be cleared',
    created_at: '2026-10-14T08:00:00Z',
    scadbuddy: {
      subject: `output:${outputs[0]!.id}`,
      output_id: outputs[0]!.id,
      library_file_id: null,
      slug: outputs[0]!.slug,
      run_id: 'run-reagan',
      archive_id: null,
      matched_by: 'queue_item',
    },
    picture: `/api/v1/outputs/${outputs[0]!.id}/thumbnail`,
  },
  {
    id: 266,
    printer_id: 1,
    printer_name: '3DP-31B-598',
    archive_id: 35,
    archive_name: 'Luna keychain',
    status: 'completed',
    manual_start: false,
    filament_short: false,
    scadbuddy: {
      subject: 'output:luna-0002',
      output_id: 'luna-0002',
      library_file_id: null,
      slug: 'keychain',
      run_id: 'run-luna-2',
      archive_id: 35,
      matched_by: 'queue_item',
    },
    picture: '/api/v1/outputs/luna-0002/thumbnail',
  },
]

export const FARM_STATS: ArchiveStats = {
  total_prints: 104,
  successful_prints: 59,
  failed_prints: 22,
  cancelled_prints: 23,
  total_print_time_hours: 200.5,
  total_filament_grams: 4013.6,
  total_cost: 76.03,
  prints_by_filament_type: { PLA: 87, PETG: 18 },
  prints_by_printer: { '1': 104 },
  printer_names: { '1': '3DP-31B-598' },
  average_time_accuracy: 93.4,
  total_energy_kwh: 0,
  total_energy_cost: 0,
  total_wear_cost: 0,
}

let queue = PRINT_QUEUE
export function reset(): void {
  queue = PRINT_QUEUE
}

export function setPrintQueue(items: QueueItem[]): void {
  queue = items
}

export const handlers = [
  http.get('/api/v1/farm/queue', ({ request }) => {
    const status = new URL(request.url).searchParams.get('status')
    return HttpResponse.json(status ? queue.filter((item) => item.status === status) : queue)
  }),
  http.get('/api/v1/farm/stats', () => HttpResponse.json(FARM_STATS)),
  // #3744 — one item; Bambuddy's 404 for an item it no longer has, as the API maps it.
  http.get('/api/v1/farm/queue/:id', ({ params }) => {
    const id = Number(params['id'])
    const item = [...queue, ...QUEUE_ITEM_ONLY].find((row) => row.id === id)
    if (item) return HttpResponse.json(item)
    return HttpResponse.json(
      {
        type: 'https://scadbuddy.dev/problems/bambuddy-not-found',
        title: 'Not Found',
        status: 404,
        detail: `Bambuddy has no such resource when asked to read queue item ${id}: Queue item not found`,
      },
      { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
    )
  }),
]
