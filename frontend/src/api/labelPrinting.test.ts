import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { LABEL_TEMPLATES } from '../mocks/features/filament'
import { server } from '../mocks/server'
import { fits, type LabelQueue, pageSizeOf, printLabels } from './labelPrinting'

const queue = (width_mm: number | null, height_mm: number | null): LabelQueue => ({
  name: 'zebra_2x1',
  info: 'Zebra 2 x 1 in',
  make_and_model: 'Zebra ZPL Label Printer',
  state: 'idle',
  state_reasons: ['none'],
  state_message: '',
  accepting: true,
  media: 'custom_2x1in_2x1in',
  width_mm,
  height_mm,
})

const letter = { widthMm: 215.9, heightMm: 279.4 }

describe('fits (#7170 R7)', () => {
  it('takes a page within 1 mm each way, in the same orientation', () => {
    expect(fits(queue(50.8, 25.4), { widthMm: 50, heightMm: 25 })).toBe(true)
    expect(fits(queue(50.8, 25.4), { widthMm: 25.4, heightMm: 50.8 })).toBe(false)
    expect(fits(queue(50.8, 25.4), letter)).toBe(false)
  })

  it('takes no page when the queue has no size, or the page is not known', () => {
    expect(fits(queue(null, null), { widthMm: 50, heightMm: 25 })).toBe(false)
    expect(fits(queue(50.8, 25.4), null)).toBe(false)
  })
})

describe('pageSizeOf (#7170 R7)', () => {
  const saved = {
    id: 'sheet-1',
    name: 'My Letter sheet',
    layout: { page: 'letter', width_mm: 66.675, height_mm: 25.4, cols: 3, rows: 10, top_mm: 12.7, left_mm: 4.76, col_gap_mm: 3.175, row_gap_mm: 0 },
  }
  const templates = { ...LABEL_TEMPLATES, saved: [saved] } as unknown as typeof LABEL_TEMPLATES

  it('is the label for a one-label roll template', () => {
    expect(pageSizeOf({ template: 'roll_50x25' }, 'roll', templates)).toEqual({ widthMm: 50, heightMm: 25 })
    expect(pageSizeOf({ template: 'custom', width_mm: 101.6, height_mm: 152.4 }, 'roll', templates)).toEqual({ widthMm: 101.6, heightMm: 152.4 })
  })

  it('is the Letter page for a saved Letter sheet, and for the sheet layout', () => {
    expect(pageSizeOf({ template: 'saved', sheet_id: 'sheet-1' }, 'roll', templates)).toEqual(letter)
    expect(pageSizeOf({ template: 'roll_50x25' }, 'sheet', templates)).toEqual(letter)
  })

  it('is the A4 page for an A4 sheet template', () => {
    expect(pageSizeOf({ template: 'avery_l7160' }, 'roll', templates)).toEqual({ widthMm: 210, heightMm: 297 })
  })

  it('is not known while the templates are not read', () => {
    expect(pageSizeOf({ template: 'roll_50x25' }, 'roll', null)).toBeNull()
  })
})

describe('printLabels (#7170)', () => {
  it('sends the PDF as the body and leaves out a default option', async () => {
    let seen: { url: string; type: string | null; body: string } | null = null
    server.use(
      http.post('*/api/v1/label-printing/queues/:queue/jobs', async ({ request }) => {
        seen = { url: request.url, type: request.headers.get('Content-Type'), body: await request.text() }
        return HttpResponse.json({ queue: 'zebra_2x1', job_id: 7, job_state: 'pending', job_state_reasons: ['none'] }, { status: 202 })
      }),
    )
    const pdf = new Blob(['%PDF-1.4\n%%EOF\n'], { type: 'application/pdf' })
    const job = await printLabels('zebra_2x1', pdf, { media_type: 'Thermal', darkness: 20, print_rate: 'Default' })
    expect(job.job_id).toBe(7)
    expect(seen).not.toBeNull()
    const url = new URL(seen!.url)
    expect(url.pathname).toBe('/api/v1/label-printing/queues/zebra_2x1/jobs')
    expect(url.search).toBe('?media_type=Thermal&darkness=20')
    expect(seen!.type).toBe('application/pdf')
    expect(seen!.body).toBe('%PDF-1.4\n%%EOF\n')
  })
})
