import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, expect, it, onTestFinished, vi } from 'vitest'
import { OLD_PHONE, PHONE, setDevices, setStepMs } from '../../mocks/features/devices'
import { server } from '../../mocks/server'
import { resetRealtime } from '../../lib/realtime'
import { renderPage } from '../../test/utils'
import { UseMyPhone } from './UseMyPhone'
import { LIST_POLL_MS, PAIRING_POLL_MS } from './useDevices'

afterEach(() => resetRealtime())

const SPOOL_ID = '00000000-0000-4000-8000-0000000000s1'

it('reads a tag on the online phone and opens the spool the read names', async () => {
  setStepMs(10)
  let sent: unknown
  server.use(
    http.post('*/api/v1/filament/tags/reads', async ({ request }) => {
      sent = await request.json()
      return HttpResponse.json({ next: 'open_spool', spool: { id: SPOOL_ID, number: 142 }, tag: null, decoded: null, draft: null })
    }),
  )
  const { user } = renderPage(<UseMyPhone />)
  await user.click(await screen.findByRole('button', { name: 'Read a tag' }))
  const link = await screen.findByRole('link', { name: 'Open spool #142' })
  expect(link).toHaveAttribute('href', `/filament/spools/${SPOOL_ID}`)
  expect(sent).toEqual({ read_id: expect.stringMatching(/^read-/) })
})

it('scans a code on the phone and links what it names', async () => {
  setStepMs(10)
  server.use(
    http.post('*/api/v1/scan/codes', () =>
      HttpResponse.json({ id: 's1', error: null, codes: [{ format: 'qr_code', text: 'L-4', resolved: { kind: 'location', id: 'loc-4', code: 'L-4' } }] }),
    ),
  )
  const { user } = renderPage(<UseMyPhone />)
  await user.click(await screen.findByRole('button', { name: 'Scan a code' }))
  expect(await screen.findByRole('link', { name: 'Open Place L-4' })).toHaveAttribute('href', '/filament/locations/loc-4')
})

it('says the read could not be decoded when the decoder refuses it', async () => {
  setStepMs(10)
  server.use(http.post('*/api/v1/filament/tags/reads', () => HttpResponse.json({ title: 'Unprocessable', status: 422, detail: 'read_id is not known' }, { status: 422 })))
  const { user } = renderPage(<UseMyPhone />)
  await user.click(await screen.findByRole('button', { name: 'Read a tag' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The phone read it, but ScadBuddy could not decode the read')
})

it('offers the pairing code when no phone is online', async () => {
  setDevices([OLD_PHONE])
  renderPage(<UseMyPhone />)
  expect(await screen.findByRole('button', { name: 'Add a device' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Read a tag' })).not.toBeInTheDocument()
})

it('lets the person choose between two online phones', async () => {
  setDevices([PHONE, { ...OLD_PHONE, online: true }])
  renderPage(<UseMyPhone />)
  expect(await screen.findByRole('combobox', { name: 'Phone' })).toHaveTextContent(PHONE.name)
})

it('goes back to the slow poll once a phone it is pairing comes online', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  onTestFinished(() => {
    vi.useRealTimers()
  })
  setDevices([OLD_PHONE])
  let reads = 0
  const count = ({ request }: { request: Request }) => {
    if (request.method === 'GET' && new URL(request.url).pathname === '/api/v1/devices') reads += 1
  }
  server.events.on('request:start', count)
  onTestFinished(() => server.events.removeListener('request:start', count))
  const { user } = renderPage(<UseMyPhone />, { userEventOptions: { advanceTimers: vi.advanceTimersByTime } })
  await user.click(await screen.findByRole('button', { name: 'Add a device' }))
  await screen.findByRole('img', { name: 'Pairing QR code' })
  setDevices([OLD_PHONE, PHONE])
  await vi.advanceTimersByTimeAsync(PAIRING_POLL_MS)
  await screen.findByRole('button', { name: 'Read a tag' })
  const before = reads
  await vi.advanceTimersByTimeAsync(LIST_POLL_MS - 1_000)
  expect(reads).toBe(before)
  await vi.advanceTimersByTimeAsync(1_000)
  await waitFor(() => expect(reads).toBe(before + 1))
})

it('shows nothing where core does not serve the devices routes', async () => {
  let asked = false
  server.use(
    http.get('*/api/v1/devices', () => {
      asked = true
      return HttpResponse.json({ title: 'Not Found', status: 404, detail: 'Not Found' }, { status: 404 })
    }),
  )
  const { container } = renderPage(<UseMyPhone />)
  await waitFor(() => expect(asked).toBe(true))
  expect(container).toBeEmptyDOMElement()
})
