import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetBambuddyLinks, setBambuddyLinks } from '../../lib/bambuddyLinks'
import { PRINT_QUEUE, setPrintQueue } from '../../mocks/features/printQueue'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import type * as Embed from '../../lib/embed'
import { LiveJobItem } from './LiveJobItem'
import { liveJobs, QUEUE_SHOWN, queueTail } from './liveJobs'
import { QUEUE_POLL_MS, QueueNotes, usePrintQueue } from './PrintQueue'

/**
 * #3745 — the queue as the Prints page reads it: its notes, and its pending items as
 * rows of a list. `started` stands for the printers' working jobs.
 */
function PrintQueue({ started = null }: { started?: string[] | null }) {
  const queue = usePrintQueue(true, started)
  const jobs = liveJobs([], queue.items ?? [])
  return (
    <>
      <QueueNotes queue={queue} tail={queueTail(queue.items ?? [])} />
      {jobs.length > 0 && (
        <ol aria-label="Waiting to print">
          {jobs.map((job) => (
            <LiveJobItem key={job.key} job={job} view="list" />
          ))}
        </ol>
      )}
    </>
  )
}

const frame = vi.hoisted(() => ({ embedded: false }))
vi.mock('../../lib/embed', async (original) => ({
  ...(await original<typeof Embed>()),
  isEmbedded: () => frame.embedded,
}))

afterEach(() => {
  frame.embedded = false
  Object.defineProperty(document, 'referrer', { value: '', configurable: true })
  vi.restoreAllMocks()
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
      'queue-item-263',
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

  it('marks the items ScadBuddy queued, and no other (#3741)', async () => {
    renderPage(<PrintQueue />)

    const keychain = await screen.findByTestId('queue-item-259')
    expect(within(keychain).getByText('Queued from ScadBuddy')).toBeInTheDocument()
    expect(within(screen.getByTestId('queue-item-260')).queryByText('Queued from ScadBuddy')).toBeNull()
  })

  it("links ScadBuddy's items to ScadBuddy, and any other to its queue item page (#3743, #3744)", async () => {
    setBambuddyLinks({ bambuddy_url: 'http://bambuddy.local', bambuddy_web_urls: null })
    const ours = PRINT_QUEUE.find((item) => item.id === 259)!
    setPrintQueue([
      ...PRINT_QUEUE,
      { ...ours, id: 261, position: 9, archive_name: 'Luna again', scadbuddy: { ...ours.scadbuddy!, archive_id: 35 } },
    ])
    renderPage(<PrintQueue />)

    // Not archived yet: its queue item page (#3744).
    const keychain = await screen.findByTestId('queue-item-259')
    expect(within(keychain).getByRole('link', { name: 'Luna keychain' })).toHaveAttribute('href', '/prints/queue/259')
    // Archived: its print.
    expect(within(screen.getByTestId('queue-item-261')).getByRole('link', { name: 'Luna again' })).toHaveAttribute(
      'href',
      '/prints/35',
    )
    // Any other item: ScadBuddy's page of the item (#3744), in the frame, which links on
    // to Bambuddy's queue. Bambuddy has no page per queue item.
    const bracket = within(screen.getByTestId('queue-item-260')).getByRole('link', { name: 'Bracket.gcode.3mf' })
    expect(bracket).toHaveAttribute('href', '/prints/queue/260')
    expect(bracket).not.toHaveAttribute('target')
  })

  it('links an item ScadBuddy did not queue to its page without a Bambuddy address (#3744)', async () => {
    renderPage(<PrintQueue />)

    const bracket = within(await screen.findByTestId('queue-item-260')).getByRole('link', { name: 'Bracket.gcode.3mf' })
    expect(bracket).toHaveAttribute('href', '/prints/queue/260')
  })

  it("shows each row's picture: ScadBuddy's, Bambuddy's, or a placeholder (#3742)", async () => {
    renderPage(<PrintQueue />)

    const ours = within(await screen.findByTestId('queue-item-259')).getByTestId('job-picture')
    // The name is beside it, so the picture says nothing more to a screen reader.
    expect(ours.querySelector('img')).toHaveAttribute('src', '/api/v1/outputs/luna-0002/thumbnail')
    expect(ours.querySelector('img')).toHaveAttribute('alt', '')
    const theirs = within(screen.getByTestId('queue-item-260')).getByTestId('job-picture')
    expect(theirs.querySelector('img')).toHaveAttribute('src', '/api/v1/farm/library/1615/thumbnail')
    const none = within(screen.getByTestId('queue-item-263')).getByTestId('job-picture')
    expect(none.querySelector('img')).toBeNull()
    expect(within(none).getByTestId('job-picture-placeholder')).toBeInTheDocument()

    // A picture that fails to load is the placeholder, never a broken image.
    fireEvent.error(theirs.querySelector('img')!)
    expect(theirs.querySelector('img')).toBeNull()
    expect(within(theirs).getByTestId('job-picture-placeholder')).toBeInTheDocument()
  })

  it('opens Bambuddy on the hostname framing ScadBuddy (#3743)', async () => {
    setBambuddyLinks({ bambuddy_url: null, bambuddy_web_urls: 'http://bambuddy.local, http://bambuddy.lan:8000' })
    frame.embedded = true
    Object.defineProperty(document, 'referrer', { value: 'http://bambuddy.lan:8000/external/3', configurable: true })
    renderPage(<PrintQueue />)

    await screen.findByTestId('queue-item-260')
    expect(screen.getByRole('link', { name: "Bambuddy's queue" })).toHaveAttribute(
      'href',
      'http://bambuddy.lan:8000/queue',
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

  it('stays out of the way when Bambuddy is not configured, which the printer strip says (#2919)', async () => {
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
    const read = vi.spyOn(api, 'getPrintQueue')
    renderPage(<PrintQueue />)
    // Wait for the read to be refused and handled, not for a guess at how long that takes.
    await waitFor(() => expect(read).toHaveBeenCalled())
    await act(async () => {
      await read.mock.results[0]!.value.catch(() => {})
    })
    expect(screen.queryByTestId('print-queue')).toBeNull()
  })

  it('still lists the queue when the totals cannot be read (#2922)', async () => {
    server.use(http.get('/api/v1/farm/stats', () => HttpResponse.json({ detail: 'boom' }, { status: 502 })))
    const stats = vi.spyOn(api, 'getFarmStats')
    renderPage(<PrintQueue />)
    expect(await screen.findByRole('list', { name: 'Waiting to print' })).toBeInTheDocument()
    // The failed totals read, handled: not a guess at how long that takes.
    await waitFor(() => expect(stats).toHaveBeenCalled())
    await act(async () => {
      await stats.mock.results[0]!.value.catch(() => {})
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

  it('reads again at once when a printer starts a new job, and only then (#3745)', async () => {
    let reads = 0
    const listener = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === '/api/v1/farm/queue') reads += 1
    }
    server.events.on('request:start', listener)
    const { rerender } = renderPage(<PrintQueue started={null} />)
    await screen.findByTestId('queue-item-259')
    expect(reads).toBe(1)
    // The printers' first read is no news, nor a job that ends.
    rerender(<PrintQueue started={['archive:90']} />)
    rerender(<PrintQueue started={[]} />)
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    expect(reads).toBe(1)
    // A job that starts reads the queue now, not a poll later.
    rerender(<PrintQueue started={['archive:37']} />)
    await waitFor(() => expect(reads).toBe(2))
    server.events.removeListener('request:start', listener)
  })

  it('says a failed read', async () => {
    server.use(
      http.get('/api/v1/farm/queue', () => HttpResponse.json({ detail: 'could not reach Bambuddy' }, { status: 502 })),
    )
    renderPage(<PrintQueue />)
    expect(await screen.findByTestId('print-queue-error')).toHaveTextContent('could not reach Bambuddy')
  })
})
