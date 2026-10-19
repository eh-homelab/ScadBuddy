import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PRINTERS_NOW, setPrintersNow } from '../../mocks/features/printersNow'
import { server } from '../../mocks/server'
import { emitRealtime } from '../../mocks/realtime'
import { renderPage } from '../../test/utils'
import { resetBambuddyLinks, setBambuddyLinks } from '../../lib/bambuddyLinks'
import type * as Embed from '../../lib/embed'
import type { PrinterNow } from '../../api/types'
import { NOW_POLL_MS, NOW_SIGNAL_MS, NowPrinting } from './NowPrinting'
import { mergeCards } from './nowCards'

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
    // Bambuddy has no page per archive: the link names the page, and the archive sits beside it (#2930).
    expect(within(paused).getByRole('link', { name: "Bambuddy's archives" })).toHaveAttribute(
      'href',
      'http://bambuddy.local/archives',
    )
    expect(paused).toHaveTextContent("Bambuddy's archives · archive 90")

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
    expect(within(paused).getByRole('link', { name: "Bambuddy's archives" })).toHaveAttribute(
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

  it('keeps the newest read when an older one answers last (#2750 review)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let release = () => {}
    let reads = 0
    server.use(
      http.get('/api/v1/print/printers/now', async () => {
        reads += 1
        const stale = reads === 2
        const printers = PRINTERS_NOW.map((printer) =>
          printer.printer_id === 2 ? { ...printer, progress: stale ? 70 : reads === 1 ? 66 : 90 } : printer,
        )
        // The timer's read hangs until the visibility read has answered.
        if (stale) await new Promise<void>((resolve) => (release = resolve))
        return HttpResponse.json({ printers })
      }),
    )
    renderPage(<NowPrinting />)
    expect(await screen.findByText('66%')).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOW_POLL_MS)
    })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(await screen.findByText('90%')).toBeInTheDocument()
    await act(async () => {
      release()
      await vi.advanceTimersByTimeAsync(50)
    })
    expect(screen.getByTestId('printer-now-2')).toHaveTextContent('90%')
    expect(screen.queryByText('70%')).toBeNull()
  })

  it('lists an HMS code reported twice twice (#2750 review)', async () => {
    const twice = { code: '0C008042', description: 'Spaghetti detected: check the print.' }
    setPrintersNow(
      PRINTERS_NOW.map((printer) => (printer.printer_id === 1 ? { ...printer, hms: [twice, twice] } : printer)),
    )
    const errors = vi.spyOn(console, 'error')
    renderPage(<NowPrinting />)

    const list = await screen.findByTestId('printer-now-hms-1')
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(errors.mock.calls.flat().join(' ')).not.toMatch(/same key/)
    errors.mockRestore()
  })

  it('shows a LAN-only printer without a camera (#3823)', async () => {
    const lan: PrinterNow = {
      ...(PRINTERS_NOW[2] as PrinterNow),
      id: '00000000-0000-4000-8000-0000000000aa',
      path: 'native',
      printer_id: null,
      name: 'Garage',
    }
    setPrintersNow([...PRINTERS_NOW, lan])
    renderPage(<NowPrinting />)

    const card = await screen.findByTestId(`printer-now-${lan.id}`)
    expect(card).toHaveTextContent('Garage')
    expect(within(card).queryByRole('button', { name: /Camera/ })).toBeNull()
    expect(screen.getByRole('button', { name: 'Camera of 3DP-31B-598' })).toBeInTheDocument()
  })

  it("labels each card with the link that answered (#3823)", async () => {
    const lan: PrinterNow = {
      ...(PRINTERS_NOW[2] as PrinterNow),
      id: '00000000-0000-4000-8000-0000000000aa',
      path: 'native',
      printer_id: null,
      name: 'Garage',
    }
    const silent: PrinterNow = { ...lan, id: '00000000-0000-4000-8000-0000000000bb', path: 'none', name: 'Attic', activity: 'offline' }
    setPrintersNow([...PRINTERS_NOW, lan, silent])
    renderPage(<NowPrinting />)

    const card = await screen.findByTestId(`printer-now-${lan.id}`)
    expect(within(card).getByText('LAN')).toBeInTheDocument()
    expect(within(screen.getByTestId('printer-now-1')).getByText('Bambuddy')).toBeInTheDocument()
    expect(within(screen.getByTestId(`printer-now-${silent.id}`)).queryByText(/^(LAN|Bambuddy)$/)).toBeNull()
  })

  it('reads the printers again when one changes, without waiting for the poll (#3823)', async () => {
    renderPage(<NowPrinting />)
    const card = await screen.findByTestId('printer-now-2')
    expect(card).toHaveTextContent('66%')

    setPrintersNow(PRINTERS_NOW.map((printer) => (printer.printer_id === 2 ? { ...printer, progress: 80 } : printer)))
    await waitFor(() => {
      emitRealtime('printer.report', ['printers', `printer:${PRINTERS_NOW[1]!.id}`], {})
      expect(screen.getByText('80%')).toBeInTheDocument()
    })
  })

  it('counts every read toward the signal throttle, and hears only printer events (#4340 review M5)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let reads = 0
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname === '/api/v1/print/printers/now') reads += 1
    })
    renderPage(<NowPrinting />)
    await screen.findByTestId('printer-now-2')
    // Subscribed: a report reads (after the throttle from the first read).
    await waitFor(async () => {
      emitRealtime('printer.report', ['printers', `printer:${PRINTERS_NOW[1]!.id}`], {})
      await act(async () => {
        await vi.advanceTimersByTimeAsync(NOW_SIGNAL_MS)
      })
      expect(reads).toBeGreaterThan(1)
    })
    // A visibility read, then a report straight after it: the report waits out the throttle.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOW_SIGNAL_MS)
    })
    const before = reads
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => expect(reads).toBe(before + 1))
    const afterVisibility = reads
    act(() => emitRealtime('printer.report', ['printers', `printer:${PRINTERS_NOW[1]!.id}`], {}))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOW_SIGNAL_MS / 2)
    })
    expect(reads).toBe(afterVisibility)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOW_SIGNAL_MS)
    })
    await waitFor(() => expect(reads).toBe(afterVisibility + 1))
    // Another kind on the topic is not news.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOW_SIGNAL_MS)
    })
    act(() => emitRealtime('printer.other', ['printers'], {}))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOW_SIGNAL_MS * 2)
    })
    expect(reads).toBe(afterVisibility + 1)
  })

  it('is absent with no printers', async () => {
    setPrintersNow([])
    renderPage(<NowPrinting />)
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(screen.queryByTestId('now-printing')).toBeNull()
  })

  it('opens the live camera with the job status beside it, and closing it drops the stream (#981)', async () => {
    const { user } = renderPage(<NowPrinting />)
    await screen.findByTestId('printer-now-1')
    await user.click(screen.getByRole('button', { name: 'Camera of 3DP-31B-598' }))

    const dialog = await screen.findByRole('dialog', { name: '3DP-31B-598 camera' })
    expect(within(dialog).getByRole('img', { name: 'Live camera of 3DP-31B-598' })).toHaveAttribute(
      'src',
      '/api/v1/print/printers/1/camera/stream',
    )
    const status = within(dialog).getByTestId('printer-camera-status')
    expect(status).toHaveTextContent('Paused')
    expect(status).toHaveTextContent('Layer 9/90')
    expect(status).toHaveTextContent('Spaghetti detected')

    await user.click(within(dialog).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('img', { name: /Live camera/ })).toBeNull())
  })

  it('empties the live image as the dialog closes, so the browser drops the stream (#3207)', async () => {
    const { user } = renderPage(<NowPrinting />)
    await screen.findByTestId('printer-now-2')
    await user.click(screen.getByRole('button', { name: 'Camera of 3DP-77A-114' }))
    const dialog = await screen.findByRole('dialog', { name: '3DP-77A-114 camera' })
    const live = within(dialog).getByRole('img', { name: 'Live camera of 3DP-77A-114' }) as HTMLImageElement
    expect(live.getAttribute('src')).toContain('/camera/stream')

    await user.click(within(dialog).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '3DP-77A-114 camera' })).toBeNull())
    expect(live.getAttribute('src')).toBe('')
  })

  it('falls back to a snapshot with Refresh when the stream fails (#981)', async () => {
    const { user } = renderPage(<NowPrinting />)
    await screen.findByTestId('printer-now-2')
    await user.click(screen.getByRole('button', { name: 'Camera of 3DP-77A-114' }))
    const dialog = await screen.findByRole('dialog', { name: '3DP-77A-114 camera' })
    fireEvent.error(within(dialog).getByRole('img', { name: 'Live camera of 3DP-77A-114' }))

    const snapshot = within(dialog).getByRole('img', { name: 'Camera snapshot of 3DP-77A-114' })
    expect(snapshot.getAttribute('src')).toMatch(/^\/api\/v1\/print\/printers\/2\/camera\?at=\d+$/)
    expect(within(dialog).getByRole('status')).toHaveTextContent('The live stream stopped')
    await user.click(within(dialog).getByRole('button', { name: 'Try live again' }))
    expect(within(dialog).getByRole('img', { name: 'Live camera of 3DP-77A-114' })).toBeInTheDocument()
  })

  it('takes a fresh snapshot on every fallback, never a cached one (#2752 review)', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(1000)
    const { user } = renderPage(<NowPrinting />)
    await screen.findByTestId('printer-now-2')
    await user.click(screen.getByRole('button', { name: 'Camera of 3DP-77A-114' }))
    const dialog = await screen.findByRole('dialog', { name: '3DP-77A-114 camera' })
    now.mockReturnValue(2000)
    fireEvent.error(within(dialog).getByRole('img', { name: 'Live camera of 3DP-77A-114' }))
    expect(within(dialog).getByRole('img', { name: 'Camera snapshot of 3DP-77A-114' })).toHaveAttribute(
      'src',
      '/api/v1/print/printers/2/camera?at=2000',
    )

    await user.click(within(dialog).getByRole('button', { name: 'Try live again' }))
    now.mockReturnValue(3000)
    fireEvent.error(within(dialog).getByRole('img', { name: 'Live camera of 3DP-77A-114' }))
    expect(within(dialog).getByRole('img', { name: 'Camera snapshot of 3DP-77A-114' })).toHaveAttribute(
      'src',
      '/api/v1/print/printers/2/camera?at=3000',
    )
    now.mockRestore()
  })

  it("opens another printer's camera live, never first as the last one's snapshot (#2752 review)", async () => {
    const { user } = renderPage(<NowPrinting />)
    await screen.findByTestId('printer-now-2')
    await user.click(screen.getByRole('button', { name: 'Camera of 3DP-77A-114' }))
    const workshop = await screen.findByRole('dialog', { name: '3DP-77A-114 camera' })
    fireEvent.error(within(workshop).getByRole('img', { name: 'Live camera of 3DP-77A-114' }))
    expect(within(workshop).getByRole('img', { name: 'Camera snapshot of 3DP-77A-114' })).toBeInTheDocument()
    await user.click(within(workshop).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '3DP-77A-114 camera' })).toBeNull())

    const shown: string[] = []
    const observer = new MutationObserver((records) => {
      for (const record of records)
        for (const node of record.addedNodes)
          if (node instanceof Element)
            for (const img of [node, ...node.querySelectorAll('img')])
              if (img instanceof HTMLImageElement) shown.push(img.alt)
    })
    observer.observe(document.body, { childList: true, subtree: true })
    await user.click(screen.getByRole('button', { name: 'Camera of 3DP-31B-598' }))
    const other = await screen.findByRole('dialog', { name: '3DP-31B-598 camera' })
    observer.disconnect()

    expect(within(other).getByRole('img', { name: 'Live camera of 3DP-31B-598' })).toBeInTheDocument()
    expect(shown.filter((alt) => alt.startsWith('Camera snapshot'))).toEqual([])
  })

  it('closes the camera of a printer that drops out, and does not reopen it when it is back (#2752 review)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { user } = renderPage(<NowPrinting />)
    await screen.findByTestId('printer-now-2')
    await user.click(screen.getByRole('button', { name: 'Camera of 3DP-77A-114' }))
    await screen.findByRole('dialog', { name: '3DP-77A-114 camera' })

    setPrintersNow(PRINTERS_NOW.filter((printer) => printer.printer_id !== 2))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOW_POLL_MS)
    })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '3DP-77A-114 camera' })).toBeNull())

    setPrintersNow(PRINTERS_NOW)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(NOW_POLL_MS)
    })
    expect(await screen.findByTestId('printer-now-2')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: '3DP-77A-114 camera' })).toBeNull()
  })

  describe('the plate warning (#3632)', () => {
    const finished = PRINTERS_NOW.map((printer) =>
      printer.printer_id === 3
        ? { ...printer, activity: 'finished' as const, state: 'FINISH', awaiting_plate_clear: true }
        : printer,
    )
    const poll = async () => {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(NOW_POLL_MS)
      })
    }

    it('shows a placeholder while the first read is out, not nothing', async () => {
      const gate: { release?: () => void } = {}
      server.use(
        http.get('/api/v1/print/printers/now', async () => {
          await new Promise<void>((resolve) => (gate.release = resolve))
          return HttpResponse.json({ printers: finished })
        }),
      )
      renderPage(<NowPrinting />)
      expect(screen.getByTestId('now-printing')).toHaveAttribute('aria-busy', 'true')
      expect(screen.getByTestId('now-printing-loading')).toBeInTheDocument()
      await waitFor(() => expect(gate.release).toBeDefined())
      gate.release?.()
      expect(await screen.findByTestId('printer-now-plate-3')).toBeInTheDocument()
      expect(screen.queryByTestId('now-printing-loading')).toBeNull()
    })

    it('is a warning, not a footnote', async () => {
      setPrintersNow(finished)
      renderPage(<NowPrinting />)
      const warning = await screen.findByTestId('printer-now-plate-3')
      expect(warning).toHaveTextContent('Clear the plate of Spare before the next print.')
      expect(warning).toHaveClass('text-warn')
    })

    it('asks for a look when Bambuddy could not say', async () => {
      setPrintersNow(
        finished.map((printer) => (printer.printer_id === 3 ? { ...printer, awaiting_plate_clear: null } : printer)),
      )
      renderPage(<NowPrinting />)
      expect(await screen.findByTestId('printer-now-plate-3')).toHaveTextContent(
        'could not tell whether the plate of Spare is clear',
      )
      expect(screen.queryByTestId('printer-now-plate-1')).toBeNull()
    })

    it('keeps the last card through an empty read, a failed status read and a failed request', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      setPrintersNow(finished)
      renderPage(<NowPrinting />)
      expect(await screen.findByTestId('printer-now-plate-3')).toBeInTheDocument()

      setPrintersNow([])
      await poll()
      expect(await screen.findByTestId('printer-now-stale-3')).toHaveTextContent('did not list this printer')
      expect(screen.getByTestId('printer-now-3')).toHaveTextContent('Finished')
      expect(screen.getByTestId('printer-now-plate-3')).toBeInTheDocument()

      setPrintersNow(
        finished.map((printer) =>
          printer.printer_id === 3
            ? { ...printer, activity: 'unknown' as const, awaiting_plate_clear: null, error: 'Bambuddy timed out' }
            : printer,
        ),
      )
      await poll()
      await waitFor(() => expect(screen.getByTestId('printer-now-stale-3')).toHaveTextContent('Bambuddy timed out'))
      expect(screen.getByTestId('printer-now-3')).toHaveTextContent('Finished')
      expect(screen.getByTestId('printer-now-plate-3')).toHaveTextContent('Clear the plate of Spare')

      server.use(
        http.get('/api/v1/print/printers/now', () => HttpResponse.json({ detail: 'no Bambuddy' }, { status: 502 })),
      )
      await poll()
      expect(await screen.findByTestId('now-printing-error')).toHaveTextContent('no Bambuddy')
      expect(screen.getByTestId('printer-now-plate-3')).toBeInTheDocument()

      server.resetHandlers()
      setPrintersNow(PRINTERS_NOW)
      await poll()
      await waitFor(() => expect(screen.queryByTestId('printer-now-stale-3')).toBeNull())
      expect(screen.queryByTestId('printer-now-plate-3')).toBeNull()
    })

    it('replaces a failed card with the next one when there is no good one to keep', () => {
      const failed: PrinterNow = { ...(PRINTERS_NOW[0] as PrinterNow), activity: 'unknown', error: 'boom' }
      const first = mergeCards(null, [failed], 1)
      expect(first).toEqual([{ printer: failed, seenAt: 1, stale: null }])
      const again = { ...failed, error: 'boom again' }
      expect(mergeCards(first, [again], 2)).toEqual([{ printer: again, seenAt: 2, stale: null }])
    })
  })
})
