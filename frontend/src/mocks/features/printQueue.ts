import { HttpResponse, http } from 'msw'
import type { ArchiveStats, QueueItem } from '../../api/types'

/**
 * #2351 — Bambuddy's queue and the farm's totals: two waiting items (one for a
 * printer, one for any H2C, short of filament) and finished history, which the panel
 * never asks for.
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
  },
  {
    id: 205,
    printer_id: 1,
    printer_name: '3DP-31B-598',
    archive_name: 'Old print',
    status: 'completed',
    manual_start: false,
    filament_short: false,
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
]
