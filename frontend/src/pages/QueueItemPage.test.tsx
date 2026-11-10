import { screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes, useParams } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type * as embed from '../lib/embed'
import { openExternal } from '../lib/embed'
import { resetBambuddyLinks, setBambuddyLinks } from '../lib/bambuddyLinks'
import { outputs } from '../mocks/fixtures'
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
