import { HttpResponse, http } from 'msw'
import { type LabelQueue, ZEBRA_ZPL } from '../../api/labelPrinting'

/**
 * #7170 — core's label-printing routes (`tools/go/core/internal/labelprint`): the cluster's CUPS
 * server with a 2 x 1 in queue (idle) and a 4 x 6 in queue that waits for its labels (`processing`,
 * `media-needed`, the zebra backend's message, `deploy/cups/README.md`). A job answers 202 for a
 * listed queue, 422 `label_queue_unknown` for another name and 415 `label_not_pdf` for a body that
 * is not a PDF.
 */

const BASE = '/api/v1/label-printing/queues'

const seed = (): LabelQueue[] => [
  {
    name: 'S-EDCDU03-2x1',
    info: 'Zebra GK420t, 2 x 1 in labels',
    make_and_model: ZEBRA_ZPL,
    state: 'idle',
    state_reasons: ['none'],
    state_message: '',
    accepting: true,
    media: 'custom_2x1in_2x1in',
    width_mm: 50.8,
    height_mm: 25.4,
  },
  {
    name: 'S-EDCDU03-4x6',
    info: 'Zebra GK420t, 4 x 6 in labels',
    make_and_model: ZEBRA_ZPL,
    state: 'processing',
    state_reasons: ['media-needed'],
    state_message: '1.02 in labels are loaded; this queue prints 6.00 in labels. Load them and calibrate (hold Feed). Waiting.',
    accepting: true,
    media: 'na_index-4x6_4x6in',
    width_mm: 101.6,
    height_mm: 152.4,
  },
]

let queues = seed()
let nextJob = 101

export function reset() {
  queues = seed()
  nextJob = 101
}

/** Takes a queue off the mock server, as when CUPS loses it: a job sent to it is `label_queue_unknown`. */
export function removeMockLabelQueue(name: string) {
  queues = queues.filter((queue) => queue.name !== name)
}

const problem = (status: number, detail: string, reason: string) =>
  HttpResponse.json({ title: detail, status, detail, reason }, { status, headers: { 'Content-Type': 'application/problem+json' } })

export const handlers = [
  http.get(BASE, () => HttpResponse.json({ queues })),
  http.post(`${BASE}/:queue/jobs`, async ({ params, request }) => {
    const body = new Uint8Array(await request.arrayBuffer())
    if (new TextDecoder().decode(body.slice(0, 5)) !== '%PDF-') return problem(415, 'The body is not a PDF.', 'label_not_pdf')
    const queue = queues.find((one) => one.name === params.queue)
    if (!queue) return problem(422, 'The label print server has no such queue.', 'label_queue_unknown')
    return HttpResponse.json({ queue: queue.name, job_id: nextJob++, job_state: 'pending', job_state_reasons: ['none'] }, { status: 202 })
  }),
]
