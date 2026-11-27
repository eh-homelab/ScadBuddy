import { PAGE_MM } from '../components/filament/labelSheet'
import { ApiError, request } from './client'
import type { LabelTemplateChoice, LabelTemplates } from './filament'

/**
 * #7170 — the label-printing routes. Go serves them (`tools/go/core/internal/labelprint`), so
 * `pnpm gen:api`, which exports Python's OpenAPI only, has no types for them. These types are
 * written by hand from the routes' JSON (`tools/go/core/README.md`, "Label printing (#7170)").
 */

/** A CUPS queue as core lists it. The size comes from the queue's `media`, or is null. */
export interface LabelQueue {
  name: string
  info: string
  make_and_model: string
  state: 'idle' | 'processing' | 'stopped'
  state_reasons: string[]
  /** CUPS's message for the queue, such as the zebra backend's "1.02 in labels are loaded; …". */
  state_message: string
  accepting: boolean
  media: string
  width_mm: number | null
  height_mm: number | null
}

/** A Zebra ZPL queue's job options. `Saved`, -1 and `Default` keep the printer's own setting. */
export interface LabelJobOptions {
  media_type: 'Saved' | 'Thermal' | 'Direct'
  darkness: number
  print_rate: string
}

/** A job CUPS accepted (202): not yet printed. */
export interface LabelJob {
  queue: string
  job_id: number
  job_state: string
  job_state_reasons: string[]
}

/** The `make_and_model` of a queue that takes the Zebra ZPL options. */
export const ZEBRA_ZPL = 'Zebra ZPL Label Printer'

/** The options that send no attribute. */
export const DEFAULT_JOB_OPTIONS: LabelJobOptions = { media_type: 'Saved', darkness: -1, print_rate: 'Default' }

const BASE = '/label-printing/queues'

export async function labelQueues(): Promise<LabelQueue[]> {
  return (await request<{ queues: LabelQueue[] }>(BASE)).queues
}

/** Sends the PDF to the queue as the body. A default option is left out of the query. */
export function printLabels(queue: string, pdf: Blob, options: LabelJobOptions): Promise<LabelJob> {
  const query = new URLSearchParams()
  if (options.media_type !== DEFAULT_JOB_OPTIONS.media_type) query.set('media_type', options.media_type)
  if (options.darkness !== DEFAULT_JOB_OPTIONS.darkness) query.set('darkness', String(options.darkness))
  if (options.print_rate !== DEFAULT_JOB_OPTIONS.print_rate) query.set('print_rate', options.print_rate)
  const search = query.size > 0 ? `?${query}` : ''
  return request<LabelJob>(`${BASE}/${encodeURIComponent(queue)}/jobs${search}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/pdf' },
    body: pdf,
  })
}

/** The route's `reason` for a refusal (`label_queue_unknown`, `label_server_unreachable` …), or null. */
export function labelProblem(cause: unknown): string | null {
  if (!(cause instanceof ApiError)) return null
  const reason = cause.problem.reason
  return typeof reason === 'string' ? reason : null
}

type Page = { widthMm: number; heightMm: number }

const pageOf = (page: keyof typeof PAGE_MM): Page => ({ widthMm: PAGE_MM[page][0], heightMm: PAGE_MM[page][1] })

/**
 * R7 — the PDF's page: the label for `roll` with a one-label template, else the sheet's page.
 * Null while the templates are not read, and for a saved sheet the list does not have.
 */
export function pageSizeOf(choice: LabelTemplateChoice, layout: 'roll' | 'sheet', templates: LabelTemplates | null): Page | null {
  if (!templates) return null
  if (choice.template === 'saved') {
    const sheet = templates.saved.find((one) => one.id === choice.sheet_id)
    return sheet ? pageOf(sheet.layout.page) : null
  }
  if (choice.template === 'custom') {
    if (choice.width_mm == null || choice.height_mm == null) return null
    return layout === 'roll' ? { widthMm: choice.width_mm, heightMm: choice.height_mm } : pageOf('letter')
  }
  const found = templates.templates.find((one) => one.id === choice.template)
  if (!found) return null
  if (found.sheet) return pageOf(found.sheet.page)
  return layout === 'roll' ? { widthMm: found.width_mm, heightMm: found.height_mm } : pageOf('letter')
}

/** How far a queue's size may be from the page, each way, in mm. */
const FIT_MM = 1

/** R7 — the queue reports a size, and it is the page's within 1 mm each way, in the same orientation. */
export function fits(queue: LabelQueue, page: Page | null): boolean {
  if (!page || queue.width_mm == null || queue.height_mm == null) return false
  return Math.abs(queue.width_mm - page.widthMm) <= FIT_MM && Math.abs(queue.height_mm - page.heightMm) <= FIT_MM
}
