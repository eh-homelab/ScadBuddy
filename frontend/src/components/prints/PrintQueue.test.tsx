import { act, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { resetBambuddyLinks, setBambuddyLinks } from '../../lib/bambuddyLinks'
import { PRINT_QUEUE, setPrintQueue } from '../../mocks/features/printQueue'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { PrintQueue, QUEUE_POLL_MS, QUEUE_SHOWN } from './PrintQueue'

afterEach(() => {
  resetBambuddyLinks()
  vi.useRealTimers()
  Object.defineProperty(document, 'hidden', { value: false, configurable: true })
})

describe('PrintQueue (#2351)', () => {
  it("lists what is waiting to print in Bambuddy's order, and why", async () => {
    setBambuddyLinks({ bambuddy_url: 'http://bambuddy.local', bambuddy_web_urls: null })
    renderPage(<PrintQueue />)

    const list = await screen.findByRole('list', { name: 'Waiting to print' })
    // Pending only: the printing and finished items are not "up next".
    expect(within(list).getAllByRole('listitem').map((row) => row.dataset.testid)).toEqual([
      'queue-item-259',
      'queue-item-260',
    ])
    const keychain = screen.getByTestId('queue-item-259')
    expect(keychain).toHaveTextContent('Luna keychain')
    expect(keychain).toHaveTextContent('3DP-31B-598 · 45m · 12 g')
    expect(keychain).toHaveTextContent('Waits for a manual start')
    const bracket = screen.getByTestId('queue-item-260')
    expect(bracket).toHaveTextContent('Bracket.gcode.3mf')
    expect(bracket).toHaveTextContent('Any H2C')
    expect(bracket).toHaveTextContent('No active H2C printers in Workshop')
    expect(bracket).toHaveTextContent('Scheduled')
    expect(within(bracket).getByText('Not enough filament loaded for this print.')).toHaveClass('text-warn')

    expect(screen.getByRole('link', { name: "Bambuddy's queue" })).toHaveAttribute(
      'href',
      'http://bambuddy.local/queue',
    )
    const totals = await screen.findByTestId('farm-totals')
    expect(totals).toHaveTextContent('104 prints')
    // 59 of the 81 that finished; cancelled prints are not failures.
    expect(totals).toHaveTextContent('73% succeeded')
    expect(totals).toHaveTextContent('4.0 kg filament')
    expect(within(totals).getByRole('link', { name: "Bambuddy's stats" })).toHaveAttribute(
      'href',
      'http://bambuddy.local/stats',
    )
  })

  it('says when nothing is waiting', async () => {
    setPrintQueue(PRINT_QUEUE.filter((item) => item.status !== 'pending'))
    renderPage(<PrintQueue />)
    expect(await screen.findByText('Nothing is waiting to print.')).toBeInTheDocument()
  })

  it("leaves a long queue's tail to Bambuddy", async () => {
    const pending = PRINT_QUEUE.find((item) => item.id === 259)!
    setPrintQueue(Array.from({ length: QUEUE_SHOWN + 3 }, (_, i) => ({ ...pending, id: 1000 + i, position: i })))
    renderPage(<PrintQueue />)
    const list = await screen.findByRole('list', { name: 'Waiting to print' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(QUEUE_SHOWN)
    expect(screen.getByTestId('print-queue-more')).toHaveTextContent("3 more waiting in Bambuddy's queue.")
  })

  it('keeps a stable order for items with no position, after the placed ones (#2919)', async () => {
    const pending = PRINT_QUEUE.find((item) => item.id === 259)!
    setPrintQueue([
      { ...pending, id: 503, position: null },
      { ...pending, id: 501, position: null },
      { ...pending, id: 502, position: 2 },
      { ...pending, id: 504, position: 1 },
    ])
    renderPage(<PrintQueue />)
    const list = await screen.findByRole('list', { name: 'Waiting to print' })
    expect(within(list).getAllByRole('listitem').map((row) => row.dataset.testid)).toEqual([
      'queue-item-504',
      'queue-item-502',
      'queue-item-501',
      'queue-item-503',
    ])
  })

  it("names the farm's cost as a cost, since Bambuddy gives no currency (#2919)", async () => {
    renderPage(<PrintQueue />)
    const totals = await screen.findByTestId('farm-totals')
    expect(totals).toHaveTextContent('Cost 76.03')
    expect(totals).not.toHaveTextContent('spent')
  })

  it('stays out of the way when Bambuddy is not configured, which Now printing says (#2919)', async () => {
    server.use(
      http.get('/api/v1/farm/queue', () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/bambuddy-not-configured',
            title: 'Conflict',
            status: 409,
            detail: 'no Bambuddy URL is configured; set one in Settings',
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    renderPage(<PrintQueue />)
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(screen.queryByTestId('print-queue')).toBeNull()
  })

  it('still lists the queue when the totals cannot be read (#2922)', async () => {
    server.use(http.get('/api/v1/farm/stats', () => HttpResponse.json({ detail: 'boom' }, { status: 502 })))
    renderPage(<PrintQueue />)
    expect(await screen.findByRole('list', { name: 'Waiting to print' })).toBeInTheDocument()
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
    })
    expect(screen.queryByTestId('farm-totals')).toBeNull()
    expect(screen.queryByTestId('print-queue-error')).toBeNull()
  })

  it('reads again on its poll, clearing an earlier error, and not while hidden (#2922)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let reads = 0
    let failing = true
    server.use(
      http.get('/api/v1/farm/queue', () => {
        reads += 1
        return failing ? HttpResponse.json({ detail: 'could not reach Bambuddy' }, { status: 502 }) : undefined
      }),
    )
    renderPage(<PrintQueue />)
    expect(await screen.findByTestId('print-queue-error')).toBeInTheDocument()

    failing = false
    await act(async () => {
      await vi.advanceTimersByTimeAsync(QUEUE_POLL_MS)
    })
    expect(await screen.findByRole('list', { name: 'Waiting to print' })).toBeInTheDocument()
    expect(screen.queryByTestId('print-queue-error')).toBeNull()

    // Hidden: neither the poll nor anything else reads.
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    const before = reads
    await act(async () => {
      await vi.advanceTimersByTimeAsync(QUEUE_POLL_MS)
    })
    expect(reads).toBe(before)

    // Shown again: read at once, without waiting for the poll.
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => expect(reads).toBe(before + 1))
  })

  it('says a failed read', async () => {
    server.use(
      http.get('/api/v1/farm/queue', () => HttpResponse.json({ detail: 'could not reach Bambuddy' }, { status: 502 })),
    )
    renderPage(<PrintQueue />)
    expect(await screen.findByTestId('print-queue-error')).toHaveTextContent('could not reach Bambuddy')
  })
})
