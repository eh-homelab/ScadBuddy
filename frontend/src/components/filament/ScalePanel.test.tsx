import { screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { ScalePanel } from './ScalePanel'

const conflict = (detail: string, code: string) =>
  HttpResponse.json({ title: 'Conflict', status: 409, detail, code }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } })

const device = (values: Record<string, unknown>) => ({
  id: 1,
  device_id: 'sb-1',
  hostname: 'spoolbuddy-bench',
  has_nfc: true,
  nfc_ok: true,
  has_scale: true,
  scale_ok: true,
  online: true,
  ...values,
})

function serveDevices(devices: Record<string, unknown>[]) {
  server.use(
    http.get('*/api/v1/filament/rfid/status', () =>
      HttpResponse.json({ override_mode: false, state: 'off', bambuddy_auto_add: null, saved_auto_add: null, detail: null, devices }),
    ),
  )
}

describe('ScalePanel', () => {
  it('lists only the devices with a scale', async () => {
    serveDevices([device({}), device({ id: 2, device_id: 'sb-2', hostname: 'spoolbuddy-reader', has_scale: false })])
    renderPage(<ScalePanel />)
    expect(await screen.findByText('spoolbuddy-bench')).toBeInTheDocument()
    expect(screen.queryByText('spoolbuddy-reader')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Tare scale/ })).toHaveLength(1)
  })

  it('asks first, then queues the tare and says when it applies', async () => {
    const posted: string[] = []
    serveDevices([device({})])
    server.use(
      http.post('*/api/v1/filament/spoolbuddy/devices/:device/tare', ({ params }) => {
        posted.push(String(params.device))
        return HttpResponse.json({ device_id: 'sb-1', status: 'queued' })
      }),
    )
    const { user } = renderPage(<ScalePanel />)
    await user.click(await screen.findByRole('button', { name: 'Tare scale spoolbuddy-bench' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(within(dialog).getByText(/Take everything off the scale first/)).toBeInTheDocument()
    expect(posted).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Tare scale' }))
    expect(await screen.findByText(/Tare queued on spoolbuddy-bench/)).toBeInTheDocument()
    expect(posted).toEqual(['sb-1'])
  })

  it('reads the devices again after a tare, so the queued tare and a new command elsewhere show', async () => {
    let reads = 0
    server.use(
      http.get('*/api/v1/filament/rfid/status', () => {
        reads++
        const devices =
          reads === 1
            ? [device({}), device({ id: 2, device_id: 'sb-2', hostname: 'spoolbuddy-shelf' })]
            : [device({ pending_command: 'tare' }), device({ id: 2, device_id: 'sb-2', hostname: 'spoolbuddy-shelf', pending_command: 'write_tag' })]
        return HttpResponse.json({ override_mode: false, state: 'off', bambuddy_auto_add: null, saved_auto_add: null, detail: null, devices })
      }),
      http.post('*/api/v1/filament/spoolbuddy/devices/:device/tare', () => HttpResponse.json({ device_id: 'sb-1', status: 'queued' })),
    )
    const { user } = renderPage(<ScalePanel />)
    await user.click(await screen.findByRole('button', { name: 'Tare scale spoolbuddy-bench' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Tare scale' }))
    expect(await screen.findByText(/A tare is queued/)).toBeInTheDocument()
    expect(screen.getByText(/A tag write is queued/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Tare scale spoolbuddy-shelf' })).toBeDisabled()
    expect(reads).toBe(2)
  })

  it('keeps the list and says so when the read after a tare fails', async () => {
    let reads = 0
    server.use(
      http.get('*/api/v1/filament/rfid/status', () => {
        reads++
        if (reads > 1) return HttpResponse.json({ title: 'Bad Gateway', status: 502, detail: 'Bambuddy did not answer' }, { status: 502, headers: { 'Content-Type': 'application/problem+json' } })
        return HttpResponse.json({ override_mode: false, state: 'off', bambuddy_auto_add: null, saved_auto_add: null, detail: null, devices: [device({})] })
      }),
      http.post('*/api/v1/filament/spoolbuddy/devices/:device/tare', () => HttpResponse.json({ device_id: 'sb-1', status: 'queued' })),
    )
    const { user } = renderPage(<ScalePanel />)
    await user.click(await screen.findByRole('button', { name: 'Tare scale spoolbuddy-bench' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Tare scale' }))
    expect(await screen.findByText(/Could not refresh the device list/)).toBeInTheDocument()
    expect(screen.getByText('spoolbuddy-bench')).toBeInTheDocument()
    expect(screen.getByText(/Tare queued on spoolbuddy-bench/)).toBeInTheDocument()
    expect(reads).toBe(2)
  })

  it('sends nothing when the person keeps the scale as it is', async () => {
    const posted: string[] = []
    serveDevices([device({})])
    server.use(
      http.post('*/api/v1/filament/spoolbuddy/devices/:device/tare', ({ params }) => {
        posted.push(String(params.device))
        return HttpResponse.json({ device_id: 'sb-1', status: 'queued' })
      }),
    )
    const { user } = renderPage(<ScalePanel />)
    await user.click(await screen.findByRole('button', { name: 'Tare scale spoolbuddy-bench' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))
    expect(posted).toEqual([])
  })

  it("says why the server refused, as it said it", async () => {
    serveDevices([device({})])
    server.use(
      http.post('*/api/v1/filament/spoolbuddy/devices/:device/tare', () =>
        conflict('the SpoolBuddy device sb-1 has the command write_tag pending', 'device_busy'),
      ),
    )
    const { user } = renderPage(<ScalePanel />)
    await user.click(await screen.findByRole('button', { name: 'Tare scale spoolbuddy-bench' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Tare scale' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('write_tag pending')
  })

  it('says a tare is already queued, and a scale that does not answer', async () => {
    serveDevices([device({ pending_command: 'tare', scale_ok: false })])
    renderPage(<ScalePanel />)
    expect(await screen.findByText(/A tare is queued/)).toBeInTheDocument()
    expect(screen.getByText(/The scale does not answer/)).toBeInTheDocument()
  })

  it('holds the button while another command is pending on the device', async () => {
    serveDevices([device({ pending_command: 'write_tag' })])
    renderPage(<ScalePanel />)
    expect(await screen.findByRole('button', { name: 'Tare scale spoolbuddy-bench' })).toBeDisabled()
    expect(screen.getByText(/A tag write is queued/)).toBeInTheDocument()
  })

  it('says when no scale is found', async () => {
    serveDevices([device({ has_scale: false })])
    renderPage(<ScalePanel />)
    expect(await screen.findByText('No SpoolBuddy scale found in Bambuddy.')).toBeInTheDocument()
  })
})
