import { HttpResponse, http } from 'msw'
import type { ArchiveOutcome } from '../../../api/types'
import { OUTPUT_REAGAN } from './state'

/**
 * #4777 (order operations spec 2026-11-07 §3.2): Bambuddy's archives as `GET /farm/archives`
 * lists them, and what the backend reads of each for a past print's item and its `archive`
 * quote (`orders/past_prints.py`). 9101 and 9102 are one two-plate run of the output
 * `OUTPUT_REAGAN`; 9103 printed library file 120; 9104 is nobody's; 9105 failed. The
 * route's `date_from`, `date_to`, `printer_id`, `project_id`, `limit` and `offset` are
 * honoured; status and name are the browser's. Each row's `scadbuddy_print` (#4698) says
 * whether an output or a library file printed it, or a person imported it (`archiveImports`).
 */

/** What the backend reads of an archive beyond the listing: its owner and its slots. */
export interface PastPrint extends ArchiveOutcome {
  output_id?: string
  library_file_id?: number
  /** Its print run's archives, the plates in order; itself alone when it is no run's. */
  run: number[]
  filename: string
  slots: { slot_id: number; used_g: number; color: string }[]
}

const print = (id: number, fields: Partial<PastPrint>): PastPrint => ({
  id,
  print_name: `Archive ${id}`,
  status: 'completed',
  printer_id: 1,
  project_id: null,
  plate_id: 1,
  created_at: `2026-11-0${id % 10}T10:00:00Z`,
  completed_at: `2026-11-0${id % 10}T11:00:00Z`,
  print_time_seconds: 3600,
  actual_time_seconds: 3300,
  filament_used_grams: 12,
  quantity: 1,
  run_count: 1,
  successful_run_count: 1,
  failed_run_count: 0,
  run: [id],
  filename: `archive-${id}.gcode.3mf`,
  slots: [{ slot_id: 1, used_g: 12, color: '#D62828' }],
  ...fields,
})

export const PAST_PRINTS: PastPrint[] = [
  print(9105, { print_name: 'Vase', status: 'failed', printer_id: 2 }),
  print(9104, { print_name: 'Benchy', filename: 'benchy.gcode.3mf', project_id: 7 }),
  print(9103, { print_name: 'Bracket', library_file_id: 120 }),
  print(9102, { print_name: 'Name tag, plate 2', output_id: OUTPUT_REAGAN, plate_id: 2, run: [9101, 9102], slots: [{ slot_id: 1, used_g: 4, color: '#D62828' }] }),
  print(9101, { print_name: 'Name tag, plate 1', output_id: OUTPUT_REAGAN, run: [9101, 9102] }),
]

export const pastPrint = (id: number) => PAST_PRINTS.find((p) => p.id === id)

/** The native library copy of each foreign archive, so a second pick reuses it. */
export const archiveCopies = new Map<number, { id: number; filename: string }>()

const LISTED = ['id', 'print_name', 'status', 'printer_id', 'project_id', 'plate_id', 'created_at', 'completed_at', 'print_time_seconds', 'actual_time_seconds', 'filament_used_grams', 'quantity', 'run_count', 'successful_run_count', 'failed_run_count'] as const

/** #4698 — the archives a person imported onto a model, by archive id: the model's slug. */
export const archiveImports = new Map<number, string>()

export function reset(): void {
  archiveCopies.clear()
  archiveImports.clear()
}

export const handlers = [
  http.get('/api/v1/farm/archives', ({ request }) => {
    const query = new URL(request.url).searchParams
    const from = query.get('date_from')
    const to = query.get('date_to')
    const printer = query.get('printer_id')
    const project = query.get('project_id')
    const limit = Number(query.get('limit') ?? 20)
    const offset = Number(query.get('offset') ?? 0)
    const day = (p: PastPrint) => (p.created_at ?? '').slice(0, 10)
    const rows = PAST_PRINTS.filter((p) => (from === null || day(p) >= from) && (to === null || day(p) <= to))
      .filter((p) => printer === null || p.printer_id === Number(printer))
      .filter((p) => project === null || p.project_id === Number(project))
      .slice(offset, offset + limit)
      .map(
        (p): ArchiveOutcome => ({
          ...(Object.fromEntries(LISTED.map((key) => [key, p[key]])) as Omit<ArchiveOutcome, 'scadbuddy_print'>),
          // #4698: an archive an output or a library file printed is a ScadBuddy print.
          scadbuddy_print: p.output_id !== undefined || p.library_file_id !== undefined || archiveImports.has(p.id),
        }),
      )
    return HttpResponse.json(rows)
  }),
]
