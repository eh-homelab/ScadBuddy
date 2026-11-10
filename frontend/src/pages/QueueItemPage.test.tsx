import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes, useParams } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type * as embed from '../lib/embed'
import { openExternal } from '../lib/embed'
import { resetBambuddyLinks, setBambuddyLinks } from '../lib/bambuddyLinks'
import type { QueueItem } from '../api/types'
import { outputs } from '../mocks/fixtures'
import { QUEUE_ITEM_ONLY } from '../mocks/features/printQueue'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { QueueItemPage } from './QueueItemPage'

vi.mock('../lib/embed', async (original) => ({
  ...(await original<typeof embed>()),
  openExternal: vi.fn(),
  isEmbedded: vi.fn(() => false),
}))

const REAGAN = outputs[0]!

function render(id: number | string) {
  return renderPage(
    <Routes>
      <Route path="/prints/queue/:itemId" element={<QueueItemPage />} />
      <Route path="/prints/:archiveId" element={<Landed name="print" />} />
    </Routes>,
    { route: `/prints/queue/${id}` },
  )
}

/** Bambuddy answers `item` for its id. */
function serve(item: QueueItem) {
  server.use(http.get(`/api/v1/farm/queue/${item.id}`, () => HttpResponse.json(item)))
}

function Landed({ name }: { name: string }) {
  const params = useParams()
  return <div data-testid="landed">{`${name}:${Object.values(params).join('/')}`}</div>
}

beforeEach(() => {
  setBambuddyLinks({ bambuddy_url: 'http://bambuddy.local', bambuddy_web_urls: null })
  vi.mocked(openExternal).mockClear()
})

afterEach(() => {
  resetBambuddyLinks()
})

describe('QueueItemPage (#3744)', () => {
  it("shows Bambuddy's fields for an item ScadBuddy did not queue, and links on to Bambuddy's queue", async () => {
    const { user } = render(260)

    expect(await screen.findByRole('heading', { level: 1, name: 'Bracket.gcode.3mf' })).toBeInTheDocument()
    const queue = screen.getByRole('region', { name: "In Bambuddy's queue" })
    expect(queue).toHaveTextContent('Pending')
    expect(queue).toHaveTextContent('Position8')
    expect(queue).toHaveTextContent('PrinterAny H2C')
    expect(queue).toHaveTextContent('No active H2C printers in Workshop')
    expect(queue).toHaveTextContent('Estimated time1h 30m')
    expect(queue).toHaveTextContent('Estimated filament48 g')
    expect(within(queue).getByText('Not enough filament loaded for this print.')).toHaveClass('text-warn')
    expect(screen.queryByText('Queued from ScadBuddy')).toBeNull()
    expect(screen.queryByRole('region', { name: 'From ScadBuddy' })).toBeNull()
    expect(screen.getByTestId('job-picture').querySelector('img')).toHaveAttribute(
      'src',
      '/api/v1/farm/library/1615/thumbnail',
    )

    // Bambuddy has no page per queue item, so the button opens its queue page.
    await user.click(screen.getByRole('button', { name: "Open Bambuddy's queue" }))
    expect(openExternal).toHaveBeenCalledWith('http://bambuddy.local/queue')
  })

  it('shows the output, template, revision, parameters, render and trays of a job ScadBuddy queued', async () => {
    render(265)

    expect(await screen.findByRole('heading', { level: 1, name: 'Reagan.3mf' })).toBeInTheDocument()
    expect(screen.getByText('Queued from ScadBuddy')).toBeInTheDocument()
    const queue = screen.getByRole('region', { name: "In Bambuddy's queue" })
    expect(queue).toHaveTextContent('Printer3DP-31B-598')
    expect(queue).toHaveTextContent('Plate1')
    expect(queue).toHaveTextContent('Build plateTextured PEI Plate')
    expect(queue).toHaveTextContent('Waits for a manual start')

    const ours = await screen.findByRole('region', { name: 'From ScadBuddy' })
    expect(await within(ours).findByRole('link', { name: REAGAN.name! })).toHaveAttribute('href', `/edit/${REAGAN.id}`)
    expect(within(ours).getByRole('link', { name: REAGAN.slug })).toHaveAttribute('href', `/m/${REAGAN.slug}`)
    expect(await within(ours).findByRole('table', { name: 'Parameters' })).toHaveTextContent('Reagan')
    // Our render: the output's own thumbnail, not Bambuddy's.
    expect(screen.getByTestId('job-picture').querySelector('img')).toHaveAttribute(
      'src',
      `/api/v1/outputs/${REAGAN.id}/thumbnail`,
    )

    // The tray each filament slot draws from: AMS-A slot 1, AMS-B slot 2.
    const slots = within(screen.getByRole('region', { name: 'Filament slots' })).getAllByRole('listitem')
    expect(slots.map((slot) => slot.textContent)).toEqual([
      expect.stringContaining('AMS-A · slot 1'),
      expect.stringContaining('AMS-B · slot 2'),
    ])
  })

  it("names an A2L's trays by its AMS Lite, which the flat array gives by local slot", async () => {
    const reagan = QUEUE_ITEM_ONLY.find((item) => item.id === 265)!
    serve({ ...reagan, sliced_for_model: 'A2L', ams_mapping: [2] })
    render(265)

    const slots = within(await screen.findByRole('region', { name: 'Filament slots' })).getAllByRole('listitem')
    expect(slots.map((slot) => slot.textContent)).toEqual([expect.stringContaining('AMS Lite · slot 3')])
  })

  it("names an A2L's trays by its AMS Lite from the assigned printer's model when the item names none (#5908)", async () => {
    const reagan = QUEUE_ITEM_ONLY.find((item) => item.id === 265)!
    serve({ ...reagan, printer_id: 7, sliced_for_model: null, target_model: null, ams_mapping: [2] })
    let read = 0
    server.use(
      http.get('/api/v1/printers', () => {
        read += 1
        return HttpResponse.json([
          { id: 'p-h2c', serial: 'S1', name: 'H2C', model: 'H2C', bambuddy_printer_id: 1 },
          { id: 'p-a2l', serial: 'S7', name: 'Lite', model: 'A2L', bambuddy_printer_id: 7 },
        ])
      }),
    )
    render(265)

    const region = await screen.findByRole('region', { name: 'Filament slots' })
    expect(await within(region).findByText('AMS Lite · slot 3')).toBeInTheDocument()
    // Read, and maybe again once the registry's topic is subscribed (#5914); never in a loop.
    expect(read).toBeGreaterThanOrEqual(1)
    expect(read).toBeLessThanOrEqual(2)
  })

  it("reads the printer's model again when the registry changes while the page is open (#5914)", async () => {
    const reagan = QUEUE_ITEM_ONLY.find((item) => item.id === 265)!
    serve({ ...reagan, printer_id: 7, sliced_for_model: null, target_model: null, ams_mapping: [2] })
    let model = 'H2C'
    server.use(
      http.get('/api/v1/printers', () =>
        HttpResponse.json([{ id: 'p-7', serial: 'S7', name: 'Seven', model, bambuddy_printer_id: 7 }]),
      ),
    )
    render(265)

    const region = await screen.findByRole('region', { name: 'Filament slots' })
    expect(await within(region).findByText('AMS-A · slot 3')).toBeInTheDocument()

    // Someone sets the printer's model to A2L in another tab: the registry's own event.
    model = 'A2L'
    await waitFor(() => {
      emitRealtime('printer.changed', ['printers', 'printer:p-7'], {})
      expect(within(region).getByText('AMS Lite · slot 3')).toBeInTheDocument()
    })
  })

  it('keeps the AMS Lite when a later read of the registry fails (#5914)', async () => {
    const reagan = QUEUE_ITEM_ONLY.find((item) => item.id === 265)!
    serve({ ...reagan, printer_id: 7, sliced_for_model: null, target_model: null, ams_mapping: [2] })
    let failing = false
    let failed = 0
    server.use(
      http.get('/api/v1/printers', () => {
        if (!failing) {
          return HttpResponse.json([{ id: 'p-7', serial: 'S7', name: 'Seven', model: 'A2L', bambuddy_printer_id: 7 }])
        }
        failed += 1
        return HttpResponse.json(
          { title: 'Internal Server Error', status: 500, detail: 'The registry did not answer' },
          { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )
    render(265)

    const region = await screen.findByRole('region', { name: 'Filament slots' })
    expect(await within(region).findByText('AMS Lite · slot 3')).toBeInTheDocument()

    // The registry changes, and the read that follows fails: what is shown stays.
    failing = true
    await waitFor(() => {
      emitRealtime('printer.changed', ['printers', 'printer:p-7'], {})
      expect(failed).toBeGreaterThan(0)
    })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(within(region).getByText('AMS Lite · slot 3')).toBeInTheDocument()
    expect(within(region).queryByText('AMS-A · slot 3')).toBeNull()
  })

  it('takes no A2L from a registry that cannot be read (#5914)', async () => {
    const reagan = QUEUE_ITEM_ONLY.find((item) => item.id === 265)!
    serve({ ...reagan, printer_id: 7, sliced_for_model: null, target_model: null, ams_mapping: [2] })
    let read = 0
    server.use(
      http.get('/api/v1/printers', () => {
        read += 1
        return HttpResponse.json(
          { title: 'Internal Server Error', status: 500, detail: 'The registry did not answer' },
          { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )
    render(265)

    const region = await screen.findByRole('region', { name: 'Filament slots' })
    await waitFor(() => expect(read).toBeGreaterThanOrEqual(1))
    expect(await within(region).findByText('AMS-A · slot 3')).toBeInTheDocument()
    expect(within(region).queryByText(/AMS Lite/)).toBeNull()
  })

  it('reads no printers when the item names its model', async () => {
    const reagan = QUEUE_ITEM_ONLY.find((item) => item.id === 265)!
    serve({ ...reagan, sliced_for_model: 'H2C', ams_mapping: [2] })
    let read = 0
    server.use(
      http.get('/api/v1/printers', () => {
        read += 1
        return HttpResponse.json([])
      }),
    )
    render(265)

    await screen.findByRole('region', { name: 'Filament slots' })
    expect(read).toBe(0)
  })

  it('says a finished item waited for no manual start any more (#5908)', async () => {
    const reagan = QUEUE_ITEM_ONLY.find((item) => item.id === 265)!
    serve({ ...reagan, status: 'completed', manual_start: true })
    render(265)

    const queue = await screen.findByRole('region', { name: "In Bambuddy's queue" })
    expect(queue).not.toHaveTextContent('Waits for a manual start')
  })

  it('names the same id on any other printer by its AMS', async () => {
    const reagan = QUEUE_ITEM_ONLY.find((item) => item.id === 265)!
    serve({ ...reagan, sliced_for_model: 'H2C', ams_mapping: [2] })
    render(265)

    const slots = within(await screen.findByRole('region', { name: 'Filament slots' })).getAllByRole('listitem')
    expect(slots.map((slot) => slot.textContent)).toEqual([expect.stringContaining('AMS-A · slot 3')])
  })

  it('says when the output ScadBuddy queued cannot be read, rather than show no parameters', async () => {
    server.use(
      http.get(
        '/api/v1/outputs/:id',
        () =>
          HttpResponse.json(
            { title: 'Internal Server Error', status: 500, detail: 'The output store did not answer' },
            { status: 500, headers: { 'Content-Type': 'application/problem+json' } },
          ),
        { once: true },
      ),
    )
    const { user } = render(265)

    const ours = await screen.findByRole('region', { name: 'From ScadBuddy' })
    expect(await within(ours).findByRole('alert')).toHaveTextContent('The output store did not answer')
    expect(within(ours).queryByRole('table', { name: 'Parameters' })).toBeNull()

    // A second read that answers shows the parameters.
    await user.click(within(ours).getByRole('button', { name: 'Try again' }))
    expect(await within(ours).findByRole('table', { name: 'Parameters' })).toHaveTextContent('Reagan')
    expect(within(ours).queryByRole('alert')).toBeNull()
  })

  it("says a ScadBuddy item's print page opens once ScadBuddy links its archive", async () => {
    const reagan = QUEUE_ITEM_ONLY.find((item) => item.id === 265)!
    serve({ ...reagan, status: 'completed', archive_id: 140 })
    render(265)

    const printed = await screen.findByRole('status', { name: 'Printed' })
    expect(printed).toHaveTextContent('archive 140')
    expect(printed).toHaveTextContent('opens once ScadBuddy links the archive')
    expect(screen.queryByTestId('landed')).toBeNull()
  })

  it('goes on to the print once ScadBuddy linked its archive', async () => {
    render(266)

    expect(await screen.findByTestId('landed')).toHaveTextContent('print:35')
  })

  it("says a printed item that is not ScadBuddy's is in Bambuddy's archives", async () => {
    render(205)

    const printed = await screen.findByRole('status', { name: 'Printed' })
    expect(printed).toHaveTextContent('archive 107')
    expect(within(printed).getByRole('link', { name: "Bambuddy's archives" })).toHaveAttribute(
      'href',
      'http://bambuddy.local/archives',
    )
  })

  it('says an item Bambuddy no longer has is gone, not an error', async () => {
    render(999)

    expect(await screen.findByRole('heading', { level: 1, name: 'This queue item is gone' })).toBeInTheDocument()
    expect(screen.getByText(/Bambuddy no longer has queue item 999/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to the prints' })).toHaveAttribute('href', '/prints')
  })

  it('says why when the item cannot be read', async () => {
    server.use(
      http.get('/api/v1/farm/queue/:id', () =>
        HttpResponse.json(
          { title: 'Bad Gateway', status: 502, detail: 'Bambuddy did not answer' },
          { status: 502, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    render(260)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Bambuddy did not answer')
  })

  it('refuses an id that is not one without asking', async () => {
    render('abc')

    expect(await screen.findByRole('heading', { level: 1, name: 'This queue item is gone' })).toBeInTheDocument()
  })
})
