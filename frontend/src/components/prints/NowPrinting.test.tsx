import { act, screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PRINTERS_NOW, setPrintersNow } from '../../mocks/features/printersNow'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { resetBambuddyLinks, setBambuddyLinks } from '../../lib/bambuddyLinks'
import type * as Embed from '../../lib/embed'
import { NOW_POLL_MS, NowPrinting } from './NowPrinting'

const frame = vi.hoisted(() => ({ embedded: false }))
vi.mock('../../lib/embed', async (original) => ({
  ...(await original<typeof Embed>()),
  isEmbedded: () => frame.embedded,
}))

afterEach(() => {
  vi.useRealTimers()
  frame.embedded = false
  resetBambuddyLinks()
  Object.defineProperty(document, 'referrer', { value: '', configurable: true })
})

describe('NowPrinting (#977)', () => {
  it("shows each printer's job, progress, time left and HMS errors, whoever started it", async () => {
    renderPage(<NowPrinting />)

    const paused = await screen.findByTestId('printer-now-1')
    expect(paused).toHaveTextContent('3DP-31B-598')
    expect(paused).toHaveTextContent('Paused')
    expect(paused).toHaveTextContent('Bracket')
    expect(within(paused).getByRole('progressbar', { name: '3DP-31B-598 progress' })).toHaveAttribute(
      'aria-valuenow',
      '32',
    )
    expect(paused).toHaveTextContent('Layer 9/90')
    expect(paused).toHaveTextContent('1h 21m left')
    const errors = screen.getByTestId('printer-now-hms-1')
    expect(errors).toHaveTextContent('Spaghetti detected: check the print. HMS 0C008042')
    expect(errors).toHaveTextContent('No description from Bambuddy HMS 0700220000020025')
    expect(errors).toHaveClass('text-warn')
    // Started from Bambuddy: its archive there, not a ScadBuddy print.
    expect(within(paused).getByRole('link', { name: 'Open in Bambuddy (archive 90)' })).toHaveAttribute(
      'href',
      'http://bambuddy.local/archives',
    )

    const linked = screen.getByTestId('printer-now-2')
    expect(within(linked).getByRole('link', { name: 'View this print' })).toHaveAttribute('href', '/prints/35')
    expect(linked).toHaveTextContent('54m left')

    const idle = screen.getByTestId('printer-now-3')
    expect(idle).toHaveTextContent('Idle')
    expect(within(idle).queryByRole('progressbar')).toBeNull()
    expect(within(idle).queryByRole('link')).toBeNull()
  })

  it('opens Bambuddy on the hostname framing ScadBuddy', async () => {
    setBambuddyLinks({ bambuddy_url: null, bambuddy_web_urls: 'http://bambuddy.local, http://bambuddy.lan:8000' })
    frame.embedded = true
    Object.defineProperty(document, 'referrer', { value: 'http://bambuddy.lan:8000/external/3', configurable: true })
    renderPage(<NowPrinting />)

    const paused = await screen.findByTestId('printer-now-1')
    expect(within(paused).getByRole('link', { name: 'Open in Bambuddy (archive 90)' })).toHaveAttribute(
      'href',
      'http://bambuddy.lan:8000/archives',
    )
  })

  it('follows the printers as they change', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    renderPage(<NowPrinting />)
    const card = await screen.findByTestId('printer-now-2')
    expect(card).toHaveTextContent('66%')

    setPrintersNow(PRINTERS_NOW.map((printer) => (printer.printer_id === 2 ? { ...printer, progress: 80 } : printer)))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOW_POLL_MS)
    })
    expect(await screen.findByText('80%')).toBeInTheDocument()
  })

  it('says a failed read rather than showing nothing', async () => {
    server.use(
      http.get('/api/v1/print/printers/now', () =>
        HttpResponse.json({ detail: 'could not reach Bambuddy' }, { status: 502 }),
      ),
    )
    renderPage(<NowPrinting />)
    expect(await screen.findByTestId('now-printing-error')).toHaveTextContent('could not reach Bambuddy')
  })

  it('is absent with no printers', async () => {
    setPrintersNow([])
    renderPage(<NowPrinting />)
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(screen.queryByTestId('now-printing')).toBeNull()
  })
})
