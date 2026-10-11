import { HttpResponse, http } from 'msw'
import type { PreviewStarted, PrintRunRequest, SlicePreview, TrayAssigned } from '../../api/types'
import { H2C_FILE, assignH2cTray, h2cPlan, h2cSlice, resetH2c } from '../h2c'

/**
 * #2169 — the print dialog's background slice: started per output or library file, then
 * read until it finishes (the second read). #2164 — "yes, that spool is in the tray".
 */

const base = '/api/v1'

/** Each started slice, with what it was asked for and how often it has been read. */
const jobs = new Map<number, { request: PrintRunRequest; reads: number; h2c: boolean }>()
let nextJob = 9000

function start(request: PrintRunRequest, h2c: boolean): PreviewStarted {
  const jobId = ++nextJob
  jobs.set(jobId, { request, reads: 0, h2c })
  return { job_id: jobId, plate_id: request.plate_id ?? 1, nozzle_plan: h2c ? h2cPlan(request) : null }
}

export const handlers = [
  http.post(`${base}/print/outputs/:id/preview-slice`, async ({ request }) =>
    HttpResponse.json(start((await request.json()) as PrintRunRequest, false)),
  ),

  http.post(`${base}/print/library/:id/preview-slice`, async ({ params, request }) =>
    HttpResponse.json(start((await request.json()) as PrintRunRequest, Number(params['id']) === H2C_FILE)),
  ),

  http.get(`${base}/print/preview-slices/:job`, ({ params }) => {
    const jobId = Number(params['job'])
    const job = jobs.get(jobId)
    if (!job) return HttpResponse.json({ detail: `there is no background slice ${jobId}` }, { status: 404 })
    job.reads += 1
    const done = job.reads > 1
    if (job.h2c) return HttpResponse.json(h2cSlice(jobId, job.request, done))
    return HttpResponse.json(
      (done
        ? {
            job_id: jobId,
            status: 'completed',
            print_time_seconds: 1820,
            filament_used_g: 6.7,
            slots: (job.request.filament_plan?.slots ?? []).map((slot) => ({ slot_id: slot.slot_id, grams: 3.3 })),
            filament_changes: 6,
          }
        : { job_id: jobId, status: 'running', slots: [] }) satisfies SlicePreview,
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
