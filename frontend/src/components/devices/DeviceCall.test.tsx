import { render, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, expect, it, onTestFinished, vi } from 'vitest'
import type { CallView } from '../../api/devices'
import { OLD_PHONE, PHONE, setStepMs } from '../../mocks/features/devices'
import { emitRealtime } from '../../mocks/realtime'
import { server } from '../../mocks/server'
import { resetRealtime } from '../../lib/realtime'
import { DeviceCall, LATE_POLL_MS } from './DeviceCall'

// The real realtime client over msw's socket, as the mocked app runs it.
afterEach(() => resetRealtime())

const items = () => within(screen.getByRole('list', { name: 'What the phone did' })).getAllByRole('listitem').map((one) => one.textContent)

it('shows an nfc.read call’s events in the order the stream gives them, then hands over the read', async () => {
  setStepMs(30)
  const onEnd = vi.fn()
  render(<DeviceCall deviceId={PHONE.id} method="nfc.read" onEnd={onEnd} />)
  await waitFor(() => expect(onEnd).toHaveBeenCalled())
  expect(onEnd.mock.calls[0]?.[0]).toMatchObject({ outcome: 'done', result: { read_id: expect.stringMatching(/^read-/), uid: '9CC69CE3', complete: true } })
  await waitFor(() =>
    expect(items()).toEqual([
      'Found tag 9CC69CE3, 48 blocks.',
      'Read 8 of 48 blocks.',
      'Lost the tag after 8 blocks (sector 2: transceive failed). Hold it to the phone again.',
      'Found tag 9CC69CE3 again. Reading from block 8.',
      'Read 24 of 48 blocks.',
      'Read 48 of 48 blocks.',
      'Done.',
    ]),
  )
  expect(screen.getByRole('progressbar', { name: 'Blocks read' })).toHaveAttribute('aria-valuenow', '48')
  expect(screen.getByText('Finished.')).toBeInTheDocument()
})

it('puts the events the read replays first, then the live ones after them', async () => {
  let reads = 0
  const view: CallView = {
    call: {
      id: 'c1',
      device_id: PHONE.id,
      method: 'nfc.read',
      args: {},
      caller_kind: 'person',
      outcome: null,
      created_at: '2026-11-27T10:00:00Z',
      deadline: '2099-01-01T00:00:00Z',
      sent_at: '2026-11-27T10:00:00Z',
      ended_at: null,
      pending_action_id: null,
    },
    events: [{ id: '1', kind: 'device.tag_found', data: { call: 'c1', uid: 'AABBCCDD', blocks: 16 } }],
    result: null,
  }
  server.use(
    http.post('*/api/v1/devices/:id/calls', () => HttpResponse.json({ call_id: 'c1' }, { status: 202 })),
    http.get('*/api/v1/devices/calls/c1', () => {
      reads += 1
      return HttpResponse.json(view)
    }),
  )
  render(<DeviceCall deviceId={PHONE.id} method="nfc.read" />)
  await waitFor(() => expect(reads).toBeGreaterThan(0))
  await screen.findByText('Found tag AABBCCDD, 16 blocks.')
  emitRealtime('device.tag_progress', ['device:call:c1'], { call: 'c1', read: 4, of: 16 })
  emitRealtime('device.tag_progress', ['device:call:c1'], { call: 'c1', read: 16, of: 16 })
  await waitFor(() => expect(items()).toEqual(['Found tag AABBCCDD, 16 blocks.', 'Read 4 of 16 blocks.', 'Read 16 of 16 blocks.']))
  emitRealtime('device.call_failed', ['device:call:c1'], { call: 'c1', outcome: 'timeout' })
  await screen.findByText('The device did not finish in time. Try again.')
  expect(screen.getByText('Stopped.')).toBeInTheDocument()
})

it('keeps reading a call past its deadline until core ends it', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  onTestFinished(() => {
    vi.useRealTimers()
  })
  let outcome: CallView['call']['outcome'] = null
  server.use(
    http.post('*/api/v1/devices/:id/calls', () => HttpResponse.json({ call_id: 'c2' }, { status: 202 })),
    http.get('*/api/v1/devices/calls/c2', () =>
      HttpResponse.json({
        call: {
          id: 'c2',
          device_id: PHONE.id,
          method: 'code.scan',
          args: {},
          caller_kind: 'person',
          outcome,
          created_at: '2026-11-27T10:00:00Z',
          deadline: new Date(Date.now() - 1_000).toISOString(),
          sent_at: '2026-11-27T10:00:00Z',
          ended_at: null,
          pending_action_id: null,
        },
        events: [],
        result: null,
      }),
    ),
  )
  render(<DeviceCall deviceId={PHONE.id} method="code.scan" />)
  await screen.findByText('Point the phone’s camera at the code and take the photo.')
  await vi.advanceTimersByTimeAsync(LATE_POLL_MS)
  expect(screen.queryByText('Stopped.')).not.toBeInTheDocument()
  outcome = 'timeout'
  await vi.advanceTimersByTimeAsync(LATE_POLL_MS)
  await screen.findByText('Stopped.')
  expect(screen.getByRole('alert')).toHaveTextContent('The device did not finish in time. Try again.')
})

it('says the device is offline when the call is refused with 409', async () => {
  const onEnd = vi.fn()
  render(<DeviceCall deviceId={OLD_PHONE.id} method="code.scan" onEnd={onEnd} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('The device is offline. Open the ScadBuddy app on it, then try again.')
  expect(onEnd).toHaveBeenCalledWith(expect.objectContaining({ outcome: 'offline', result: null }))
})
