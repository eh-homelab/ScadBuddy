import { HttpResponse, http } from 'msw'
import type { PreviewStarted, PrintRunRequest, SlicePreview, TrayAssigned } from '../../api/types'
import { demoToolpath } from '../toolpath'
import { H2C_FILE, assignH2cTray, h2cPlan, h2cSlice, resetH2c } from '../h2c'
import { mockSettings } from '../handlers'
import { acceptOperation } from './operations'

/**
 * #2169 — the print dialog's background slice: started per output or library file, then
 * read until it finishes (the second read). #2164 — "yes, that spool is in the tray".
 */

const base = '/api/v1'

/** Each started slice, with what it was asked for and how often it has been read. */
const jobs = new Map<number, { request: PrintRunRequest; reads: number; h2c: boolean }>()
let nextJob = 9000

/** Who slices, as Settings say: a native start is read with `?slicer=native` (Task 14). */
function slicer(): PreviewStarted['slicer'] {
  return mockSettings().slicer_backend === 'native' ? 'native' : 'bambuddy'
}

function start(request: PrintRunRequest, h2c: boolean): PreviewStarted {
  const jobId = ++nextJob
  jobs.set(jobId, { request, reads: 0, h2c })
  return {
    job_id: jobId,
    plate_id: request.plate_id ?? 1,
    nozzle_plan: h2c ? h2cPlan(request) : null,
    slicer: slicer(),
  }
}

export const handlers = [
  // Since #2700 the backend answers a preview-slice start 202 with its running operation
  // as soon as it is recorded, and the dialog's `command()` follows it (#2824). Each
  // start's operation is recorded in `operations.ts`, finished with the slice's
  // `PreviewStarted`.
  http.post(`${base}/print/outputs/:id/preview-slice`, async ({ params, request }) =>
    acceptOperation('preview_slice', `output:${String(params['id'])}`, {
      ...start((await request.json()) as PrintRunRequest, false),
    }),
  ),

  http.post(`${base}/print/library/:id/preview-slice`, async ({ params, request }) =>
    acceptOperation('preview_slice', `library:${String(params['id'])}`, {
      ...start((await request.json()) as PrintRunRequest, Number(params['id']) === H2C_FILE),
    }),
  ),

  // #2072 — a finished slice's toolpath.
  http.get(`${base}/print/:kind/:id/preview-slices/:job/toolpath`, ({ params }) =>
    jobs.has(Number(params['job']))
      ? new HttpResponse(demoToolpath(), { headers: { 'Content-Type': 'application/octet-stream' } })
      : HttpResponse.json({ detail: 'there is no background slice' }, { status: 404 }),
  ),

  http.get(`${base}/print/:kind/:id/preview-slices/:job`, ({ params, request }) => {
    const jobId = Number(params['job'])
    const job = jobs.get(jobId)
    if (!job) return HttpResponse.json({ detail: `there is no background slice ${jobId}` }, { status: 404 })
    job.reads += 1
    const done = job.reads > 1
    const native = new URL(request.url).searchParams.get('slicer') === 'native'
    // The native slicer's read says how far it has got while it runs (Task 14).
    if (native && !done) {
      return HttpResponse.json({
        job_id: jobId,
        status: 'running',
        slots: [],
        progress: 40,
        slicer: 'native',
        warnings: [],
      } satisfies SlicePreview)
    }
    if (job.h2c) return HttpResponse.json({ ...h2cSlice(jobId, job.request, done), slicer: native ? 'native' : 'bambuddy' })
    return HttpResponse.json(
      (done
        ? {
            job_id: jobId,
            status: 'completed',
            print_time_seconds: 1820,
            filament_used_g: 6.7,
            slots: (job.request.filament_plan?.slots ?? []).map((slot) => ({ slot_id: slot.slot_id, grams: 3.3 })),
            filament_changes: 6,
            slicer: native ? 'native' : 'bambuddy',
            warnings: [],
          }
        : { job_id: jobId, status: 'running', slots: [], slicer: 'bambuddy', warnings: [] }) satisfies SlicePreview,
    )
  }),

  http.post(`${base}/print/printers/:printer/trays/:ams/:tray/spool`, async ({ params, request }) => {
    const { spool_id: spoolId } = (await request.json()) as { spool_id: number }
    assignH2cTray(spoolId)
    return HttpResponse.json({
      spool_id: spoolId,
      printer_id: Number(params['printer']),
      ams_id: Number(params['ams']),
      tray_id: Number(params['tray']),
    } satisfies TrayAssigned)
  }),
]

export function reset(): void {
  jobs.clear()
  resetH2c()
}
