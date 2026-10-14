import { screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { resetBambuddyLinks, setBambuddyLinks } from '../../lib/bambuddyLinks'
import { PRINT_QUEUE, setPrintQueue } from '../../mocks/features/printQueue'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { PrintQueue, QUEUE_SHOWN } from './PrintQueue'

afterEach(() => {
  resetBambuddyLinks()
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

  it('says a failed read', async () => {
    server.use(
      http.get('/api/v1/farm/queue', () => HttpResponse.json({ detail: 'could not reach Bambuddy' }, { status: 502 })),
    )
    renderPage(<PrintQueue />)
    expect(await screen.findByTestId('print-queue-error')).toHaveTextContent('could not reach Bambuddy')
  })
})
