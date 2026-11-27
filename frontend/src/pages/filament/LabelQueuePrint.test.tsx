import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import type { LabelsRequest } from '../../api/filament'
import { labelQueues, ZEBRA_ZPL } from '../../api/labelPrinting'
import { mockSettings } from '../../mocks/handlers'
import { removeMockLabelQueue } from '../../mocks/features/labelPrinting'
import { spoolId } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { choose, optionsOf } from '../../test/select'
import { renderPage } from '../../test/utils'
import { LabelQueuePrint } from './LabelQueuePrint'

const BODY: LabelsRequest = { spool_ids: [spoolId(1)], location_ids: [], template: { template: 'roll_50x25' }, layout: 'roll', fields: ['number', 'qr'] }
const TWO_BY_ONE = { widthMm: 50, heightMm: 25 }
const FOUR_BY_SIX = { widthMm: 101.6, heightMm: 152.4 }
const LETTER = { widthMm: 215.9, heightMm: 279.4 }

const queuePicker = () => screen.findByRole('combobox', { name: 'Label queue' })

/** Every labels file the control asked for, and every job it sent. */
function watch() {
  const files: unknown[] = []
  const jobs: string[] = []
  const listen = ({ request }: { request: Request }) => {
    const url = new URL(request.url)
    if (request.method === 'POST' && url.pathname.endsWith('/filament/labels')) void request.clone().json().then((body) => files.push(body))
    if (request.method === 'POST' && url.pathname.includes('/label-printing/')) jobs.push(`${url.pathname}${url.search}`)
  }
  server.events.on('request:start', listen)
  stops.push(() => server.events.removeListener('request:start', listen))
  return { files, jobs }
}

const stops: (() => void)[] = []
afterEach(() => {
  for (const stop of stops.splice(0)) stop()
})

describe('LabelQueuePrint (#7170 R7, R9)', () => {
  it('lists the queues and pre-picks the stored one', async () => {
    await api.putSettings({ filament_label_queue: 'S-EDCDU03-2x1' })
    renderPage(<LabelQueuePrint body={BODY} page={TWO_BY_ONE} />)
    await waitFor(async () => expect(await queuePicker()).toHaveTextContent('Zebra GK420t, 2 x 1 in labels'))
    expect(screen.getByText('Idle')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Print to label printer' })).toBeEnabled()
  })

  it('pre-picks the only queue that fits when none is stored', async () => {
    renderPage(<LabelQueuePrint body={BODY} page={FOUR_BY_SIX} />)
    await waitFor(async () => expect(await queuePicker()).toHaveTextContent('Zebra GK420t, 4 x 6 in labels'))
    expect(screen.getByText(/1\.02 in labels are loaded/)).toBeInTheDocument()
  })

  it('offers no label queue for a sheet', async () => {
    renderPage(<LabelQueuePrint body={{ ...BODY, layout: 'sheet' }} page={LETTER} />)
    expect(await screen.findByText(/No label queue fits this page/)).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Label queue' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Print to label printer' })).not.toBeInTheDocument()
  })

  it('shows the options only for a Zebra ZPL queue', async () => {
    const other = {
      name: 'office-letter',
      info: 'Office laser',
      make_and_model: 'Generic PostScript Printer',
      state: 'idle',
      state_reasons: ['none'],
      state_message: '',
      accepting: true,
      media: 'na_letter_8.5x11in',
      width_mm: 215.9,
      height_mm: 279.4,
    }
    server.use(http.get('*/api/v1/label-printing/queues', () => HttpResponse.json({ queues: [other] })))
    const { rerender } = renderPage(<LabelQueuePrint body={BODY} page={LETTER} />)
    await waitFor(async () => expect(await queuePicker()).toHaveTextContent('Office laser'))
    expect(screen.queryByRole('combobox', { name: 'Print mode' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Darkness' })).not.toBeInTheDocument()

    server.use(http.get('*/api/v1/label-printing/queues', () => HttpResponse.json({ queues: [{ ...other, make_and_model: ZEBRA_ZPL }] })))
    rerender(<LabelQueuePrint key="again" body={BODY} page={LETTER} />)
    expect(await screen.findByRole('combobox', { name: 'Print mode' })).toHaveTextContent('Printer default')
    expect(screen.getByRole('combobox', { name: 'Darkness' })).toHaveTextContent('Printer default')
    expect(screen.getByRole('combobox', { name: 'Speed' })).toHaveTextContent('Printer default')
  })

  it('saves the queue and each option', async () => {
    const listed = (await labelQueues())[0]!
    server.use(
      http.get('*/api/v1/label-printing/queues', () => HttpResponse.json({ queues: [{ ...listed, name: 'ZD421-2x1', info: 'Zebra ZD421, 2 x 1 in labels' }, listed] })),
    )
    const { user } = renderPage(<LabelQueuePrint body={BODY} page={TWO_BY_ONE} />)
    // Two queues fit and none is stored, so none is picked until the person picks one.
    const picker = await queuePicker()
    expect(picker).toHaveTextContent('Choose a queue')
    expect(await optionsOf(user, picker)).toEqual(['Zebra ZD421, 2 x 1 in labels Idle', 'Zebra GK420t, 2 x 1 in labels Idle'])
    expect(screen.getByRole('button', { name: 'Print to label printer' })).toBeDisabled()
    await choose(user, picker, 'Zebra GK420t, 2 x 1 in labels Idle')
    await waitFor(() => expect(mockSettings().filament_label_queue).toBe('S-EDCDU03-2x1'))

    await choose(user, screen.getByRole('combobox', { name: 'Print mode' }), 'Thermal transfer')
    await waitFor(() => expect(mockSettings().filament_label_job_options).toEqual({ media_type: 'Thermal', darkness: -1, print_rate: 'Default' }))
    await choose(user, screen.getByRole('combobox', { name: 'Darkness' }), '20')
    await waitFor(() => expect(mockSettings().filament_label_job_options).toEqual({ media_type: 'Thermal', darkness: 20, print_rate: 'Default' }))
    await choose(user, screen.getByRole('combobox', { name: 'Speed' }), '4 in/s')
    await waitFor(() => expect(mockSettings().filament_label_job_options).toEqual({ media_type: 'Thermal', darkness: 20, print_rate: '4' }))
  })

  it('says sent, with the queue\'s message', async () => {
    await api.putSettings({ filament_label_job_options: { media_type: 'Direct', darkness: 12, print_rate: 'Default' } })
    const seen = watch()
    const { user } = renderPage(<LabelQueuePrint body={BODY} page={FOUR_BY_SIX} />)
    await waitFor(async () => expect(await queuePicker()).toHaveTextContent('Zebra GK420t, 4 x 6 in labels'))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Print mode' })).toHaveTextContent('Direct thermal'))
    await user.click(screen.getByRole('button', { name: 'Print to label printer' }))
    const status = await screen.findByRole('status')
    await waitFor(() => expect(status).toHaveTextContent('Sent to Zebra GK420t, 4 x 6 in labels (job 101).'))
    expect(within(status).getByText(/1\.02 in labels are loaded; this queue prints 6\.00 in labels/)).toBeInTheDocument()
    expect(seen.files).toEqual([{ ...BODY, format: 'pdf' }])
    expect(seen.jobs).toEqual(['/api/v1/label-printing/queues/S-EDCDU03-4x6/jobs?media_type=Direct&darkness=12'])
  })

  it('refreshes the queues after label_queue_unknown', async () => {
    const { user } = renderPage(<LabelQueuePrint body={BODY} page={TWO_BY_ONE} />)
    await waitFor(async () => expect(await queuePicker()).toHaveTextContent('Zebra GK420t, 2 x 1 in labels'))
    removeMockLabelQueue('S-EDCDU03-2x1')
    await user.click(screen.getByRole('button', { name: 'Print to label printer' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The print server no longer has the queue Zebra GK420t, 2 x 1 in labels.')
    // The list is read again: no queue that is left fits a 2 x 1 page.
    expect(await screen.findByText(/No label queue fits this page/)).toBeInTheDocument()
  })

  it('shows Unknown with a neutral dot when CUPS gives no state', async () => {
    const listed = (await labelQueues())[0]!
    server.use(http.get('*/api/v1/label-printing/queues', () => HttpResponse.json({ queues: [{ ...listed, state: '' }] })))
    renderPage(<LabelQueuePrint body={BODY} page={TWO_BY_ONE} />)
    await waitFor(async () => expect(await queuePicker()).toHaveTextContent('Zebra GK420t, 2 x 1 in labels'))
    const word = screen.getByText('Unknown')
    expect(word.parentElement?.previousElementSibling).toHaveClass('bg-muted-foreground')
  })

  it('shows each queue\'s state and message in the picker', async () => {
    const listed = (await labelQueues())[0]!
    const stopped = { ...listed, name: 'ZD421-2x1', info: 'Zebra ZD421, 2 x 1 in labels', state: 'stopped', state_message: 'Paused by the operator.' }
    server.use(http.get('*/api/v1/label-printing/queues', () => HttpResponse.json({ queues: [stopped, listed] })))
    const { user } = renderPage(<LabelQueuePrint body={BODY} page={TWO_BY_ONE} />)
    const picker = await queuePicker()
    expect(await optionsOf(user, picker)).toEqual(['Zebra ZD421, 2 x 1 in labels Stopped. Paused by the operator.', 'Zebra GK420t, 2 x 1 in labels Idle'])
  })

  it('says why no queue shows while the page is unknown', async () => {
    const { rerender } = renderPage(<LabelQueuePrint body={BODY} page={null} />)
    expect(await screen.findByText('Reading the label sizes…')).toBeInTheDocument()
    rerender(<LabelQueuePrint body={BODY} page={null} sizeUnset />)
    expect(await screen.findByText('Choose a label size first.')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Label queue' })).not.toBeInTheDocument()
  })
})
