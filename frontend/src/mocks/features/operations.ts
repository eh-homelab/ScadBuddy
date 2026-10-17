import { HttpResponse, http } from 'msw'
import type { Operation, OperationAccepted } from '../../api/types'

/**
 * `GET /operations/:id`, for every mock that answers 202 with an operation (#3457). A
 * feature records its operation with `acceptOperation`, already finished with its result,
 * so the client's follow reads it at once; an id nobody recorded is a 404.
 */

const base = '/api/v1'

const operations = new Map<string, Operation>()
let nextOperation = 0

/** The 202 a start answers: the operation running, recorded as finished with `result`. */
export function acceptOperation(
  kind: Operation['kind'],
  subject: string,
  result: NonNullable<Operation['result']>,
): HttpResponse<OperationAccepted> {
  const created = new Date().toISOString()
  const id = `op${(++nextOperation).toString(16).padStart(30, '0')}`
  const op = { id, kind, subject, status: 'running', created_at: created } as const
  operations.set(id, { ...op, status: 'succeeded', finished_at: created, result: { ...result } })
  return HttpResponse.json({ ...op, repeated: false }, { status: 202 })
}

export const handlers = [
  http.get(`${base}/operations/:id`, ({ params }) => {
    const op = operations.get(String(params['id']))
    if (!op) {
      return HttpResponse.json(
        { title: 'Not Found', status: 404, detail: `there is no operation ${String(params['id'])}` },
        { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
      )
    }
    return HttpResponse.json(op)
  }),
]

export function reset(): void {
  operations.clear()
  nextOperation = 0
}
