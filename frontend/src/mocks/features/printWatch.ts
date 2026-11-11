import { HttpResponse, http } from 'msw'
import type { AutoImport, LanPrintRecord } from '../../api/types'
import { mockModels, problem } from '../handlers'
import { archiveImports } from './orders/pastPrints'

/**
 * #4699 — the print watcher's records (`GET /print-imports/auto`) over the past prints of
 * `orders/pastPrints.ts`. Unsorted imports holds 9104 (Benchy), with its slicer project
 * recorded as the source, and 9105 (Vase), for which no source had the 3MF. An inbox
 * or dismissed print that an import (`printImports.ts`) links since is in no list, as the
 * route does. A dismiss (`POST /print-imports/auto/{id}/dismiss`) marks a print `dismissed`.
 * The LAN-only printers' records (#6041, `/print-imports/auto/lan`) are below, apart.
 */

export const NO_MODEL_MATCHED = 'No model matched the job name with confidence'
export const NO_SOURCE = "No allowed source had the print's 3MF: the archive has no file"

function watcherRows(): AutoImport[] {
  return [
    {
      archive_id: 9105,
      printer_id: 2,
      print_name: 'Vase',
      state: 'inbox',
      source: null,
      reason: NO_SOURCE,
      suggested: [],
      seen_at: '2026-11-10T21:05:00Z',
    },
    {
      archive_id: 9104,
      printer_id: 1,
      print_name: 'Benchy',
      state: 'inbox',
      source: 'archive_source',
      filename: 'benchy.3mf',
      size: 412_330,
      reason: NO_MODEL_MATCHED,
      suggested: [],
      seen_at: '2026-11-10T20:05:00Z',
    },
  ]
}

/** #6041 — the LAN-only printer of `printers.ts` (no Bambuddy link). */
export const LAN_PRINTER = { id: '00000000-0000-4000-8000-0000000000aa', name: 'Garage' }
export const NO_FILE = 'no source had the 3MF (printer: not allowed in Settings)'

/**
 * #6041 — the watcher's records of prints on LAN-only printers (`GET /print-imports/auto/lan`):
 * record 7 (cube), kept from the printer and in the inbox, and record 6 (vase), with no file
 * kept, so a link refuses it (409). Their ids are their own, apart from the archive ids.
 */
function lanRows(): LanPrintRecord[] {
  return [
    {
      id: 7,
      printer_id: LAN_PRINTER.id,
      printer_name: LAN_PRINTER.name,
      subtask_name: 'cube',
      started_at: '2026-11-11T12:00:00Z',
      state: 'inbox',
      source: 'printer',
      library_file_id: 1_000_000_041,
      filename: 'cube.gcode.3mf',
      reason: NO_MODEL_MATCHED,
      suggested: ['name-keychain'],
      seen_at: '2026-11-11T12:00:05Z',
    },
    {
      id: 6,
      printer_id: LAN_PRINTER.id,
      printer_name: LAN_PRINTER.name,
      subtask_name: 'vase',
      started_at: '2026-11-11T11:00:00Z',
      state: 'inbox',
      source: null,
      reason: NO_FILE,
      suggested: [],
      seen_at: '2026-11-11T11:00:05Z',
    },
  ]
}

let rows = watcherRows()
let lan = lanRows()

export function reset(): void {
  rows = watcherRows()
  lan = lanRows()
}

/** For a test: the LAN records the mocks hold now (a link or a dismiss changes them). */
export function lanRecords(): readonly LanPrintRecord[] {
  return lan
}

/** For a test: one more LAN record. */
export function addLanRecord(row: LanPrintRecord): void {
  lan = [...lan, row]
}

export const handlers = [
  http.get('/api/v1/print-imports/auto', ({ request }) => {
    const query = new URL(request.url).searchParams
    const state = query.get('state')
    const before = Number(query.get('before') ?? 0)
    const limit = Number(query.get('limit') ?? 50)
    const listed = rows
      // Any row but a linked one that a link names since is left out, as `_page` does.
      .filter((row) => row.state === 'linked' || !archiveImports.has(row.archive_id))
      .filter((row) => state === null || row.state === state)
      .filter((row) => !before || row.archive_id < before)
      .sort((a, b) => b.archive_id - a.archive_id)
      .slice(0, limit)
    return HttpResponse.json(listed satisfies AutoImport[])
  }),

  http.post('/api/v1/print-imports/auto/:archiveId/dismiss', ({ params }) => {
    const id = Number(params['archiveId'])
    const row = rows.find((r) => r.archive_id === id)
    if (!row) return problem(404, 'Not Found', `the print watcher has no print for archive ${id}`)
    if (row.state === 'dismissed') return HttpResponse.json(row satisfies AutoImport)
    const owner = archiveImports.get(id) ?? (row.state === 'linked' ? row.slug : undefined)
    if (owner) return problem(409, 'Conflict', `archive ${id} is a print of model:${owner}, not in Unsorted imports`)
    const dismissed: AutoImport = { ...row, state: 'dismissed', slug: null, matched_by: null, dismissed_at: new Date().toISOString() }
    rows = rows.map((r) => (r.archive_id === id ? dismissed : r))
    return HttpResponse.json(dismissed satisfies AutoImport)
  }),

  http.get('/api/v1/print-imports/auto/lan', ({ request }) => {
    const query = new URL(request.url).searchParams
    const state = query.get('state')
    const slug = query.get('slug')
    const before = Number(query.get('before') ?? 0)
    const limit = Number(query.get('limit') ?? 50)
    const listed = lan
      .filter((row) => state === null || row.state === state)
      .filter((row) => slug === null || (row.state === 'linked' && row.slug === slug))
      .filter((row) => !before || (row.id ?? 0) < before)
      .sort((a, b) => (b.id ?? 0) - (a.id ?? 0))
      .slice(0, limit)
    return HttpResponse.json(listed satisfies LanPrintRecord[])
  }),

  http.post('/api/v1/print-imports/auto/lan/:id/dismiss', ({ params }) => {
    const id = Number(params['id'])
    const row = lan.find((r) => r.id === id)
    if (!row) return problem(404, 'Not Found', `the print watcher has no LAN print ${id}`)
    if (row.state === 'dismissed') return HttpResponse.json(row satisfies LanPrintRecord)
    if (row.state === 'linked')
      return problem(409, 'Conflict', `LAN print ${id} is a print of model '${row.slug}', not in Unsorted imports`)
    const dismissed: LanPrintRecord = { ...row, state: 'dismissed', dismissed_at: new Date().toISOString() }
    lan = lan.map((r) => (r.id === id ? dismissed : r))
    return HttpResponse.json(dismissed satisfies LanPrintRecord)
  }),

  http.post('/api/v1/print-imports/auto/lan/:id/link', async ({ params, request }) => {
    const id = Number(params['id'])
    const { slug } = (await request.json()) as { slug: string }
    if (!mockModels().some((m) => m.slug === slug)) return problem(404, 'Not Found', `no model named '${slug}'`)
    const row = lan.find((r) => r.id === id)
    if (!row) return problem(404, 'Not Found', `the print watcher has no LAN print ${id}`)
    if (row.state === 'linked' && row.slug === slug) return HttpResponse.json(row satisfies LanPrintRecord)
    if (row.state === 'linked')
      return problem(409, 'Conflict', `LAN print ${id} is a print of model '${row.slug}', not in Unsorted imports`)
    if (row.source == null)
      return problem(409, 'Conflict', `LAN print ${id} has no kept 3MF (${row.reason}): a model link needs the file`)
    const linked: LanPrintRecord = { ...row, state: 'linked', slug, matched_by: 'import', reason: null, dismissed_at: null }
    lan = lan.map((r) => (r.id === id ? linked : r))
    return HttpResponse.json(linked satisfies LanPrintRecord)
  }),
]
