import { HttpResponse, http } from 'msw'
import type { AutoImport } from '../../api/types'
import { problem } from '../handlers'
import { archiveImports } from './orders/pastPrints'

/**
 * #4699 — the print watcher's records (`GET /print-imports/auto`) over the past prints of
 * `orders/pastPrints.ts`. Unsorted imports holds 9104 (Benchy), with its slicer project
 * recorded as the source, and 9105 (Vase), for which no source had the 3MF. An inbox
 * print that an import (`printImports.ts`) links since leaves the inbox, as the route
 * does. A dismiss (`POST /print-imports/auto/{id}/dismiss`) marks a print `dismissed`.
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

let rows = watcherRows()

export function reset(): void {
  rows = watcherRows()
}

export const handlers = [
  http.get('/api/v1/print-imports/auto', ({ request }) => {
    const query = new URL(request.url).searchParams
    const state = query.get('state')
    const before = Number(query.get('before') ?? 0)
    const limit = Number(query.get('limit') ?? 50)
    const listed = rows
      .filter((row) => row.state !== 'inbox' || !archiveImports.has(row.archive_id))
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
]
