import { HttpResponse, http } from 'msw'
import type { Association, AssociationRequest } from '../../api/types'
import { problem } from '../handlers'
import { libraryFiles } from './library'

/**
 * #6395 — library files' associations, kept in memory. A file's listing has its own and
 * those naming it as their target; a model's those naming the model. Nothing is implied
 * here: a test that wants an implied row adds it with `addAssociation`.
 */

const base = '/api/v1'

let rows: Association[] = []
let next = 1

export function reset(): void {
  rows = []
  next = 1
}

/** A row as the backend would hold it, for a test to start from. */
export function addAssociation(row: Partial<Association> & Pick<Association, 'library_file_id' | 'target' | 'relation'>): Association {
  const made: Association = {
    id: `00000000-0000-4000-8000-${String(next++).padStart(12, '0')}`,
    library_file_name: libraryFiles.find((file) => file.id === row.library_file_id)?.filename ?? `file-${row.library_file_id}.3mf`,
    target_name: null,
    note: '',
    created_at: '2026-11-15T10:00:00Z',
    created_by: { kind: 'browser', principal: null, session: null, run_id: null },
    implied_by: null,
    ...row,
  }
  rows.push(made)
  return made
}

export const handlers = [
  http.get(`${base}/print/library/:fileId/associations`, ({ params }) => {
    const id = Number(params.fileId)
    return HttpResponse.json({
      associations: rows.filter((row) => row.library_file_id === id || row.target === `library:${id}`),
    })
  }),
  http.get(`${base}/models/:slug/associations`, ({ params }) =>
    HttpResponse.json({ associations: rows.filter((row) => row.target === `model:${String(params.slug)}`) }),
  ),
  http.post(`${base}/print/library/:fileId/associations`, async ({ params, request }) => {
    const id = Number(params.fileId)
    const body = (await request.json()) as AssociationRequest
    if (body.target === `library:${id}`) return problem(422, 'Unprocessable Content', 'a file is not associated with itself')
    const same = rows.find((row) => row.library_file_id === id && row.target === body.target && row.relation === body.relation)
    if (same) {
      same.note = body.note ?? ''
      return HttpResponse.json(same)
    }
    const made = addAssociation({ library_file_id: id, target: body.target, relation: body.relation, note: body.note ?? '' })
    return HttpResponse.json(made, { status: 201 })
  }),
  http.delete(`${base}/print/library/associations/:id`, ({ params }) => {
    const before = rows.length
    rows = rows.filter((row) => row.id !== params.id)
    return rows.length < before ? new HttpResponse(null, { status: 204 }) : problem(404, 'Not Found', `no association ${String(params.id)}`)
  }),
]
