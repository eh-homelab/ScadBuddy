import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { PrinterCameraSetting } from './PrinterCameraSetting'

const LABEL = 'Let the assistant see printers’ cameras'

describe('PrinterCameraSetting (#1911)', () => {
  it('is on by default, and turns off and on at once', async () => {
    const { user } = renderPage(<PrinterCameraSetting />)
    const box = await screen.findByRole('checkbox', { name: LABEL })
    expect(box).toBeChecked()

    await user.click(box)
    await waitFor(() => expect(box).not.toBeChecked())
    expect(await api.getPrinterCameraSetting()).toEqual({ enabled: false })

    await user.click(box)
    await waitFor(() => expect(box).toBeChecked())
    expect(await api.getPrinterCameraSetting()).toEqual({ enabled: true })
  })

  it('is for the user only: an agent cannot turn the camera on for itself', async () => {
    renderPage(<PrinterCameraSetting />)
    const box = await screen.findByRole('checkbox', { name: LABEL })
    expect(box.closest('[data-agent-user-only]')).not.toBeNull()
  })

  it('shows why a save was refused, and keeps the stored value', async () => {
    server.use(
      http.put('/api/v1/ai/settings/printer-camera', () =>
        HttpResponse.json({ detail: 'changes to the printer camera tool setting must come from the ScadBuddy UI' }, { status: 403 }),
      ),
    )
    const { user } = renderPage(<PrinterCameraSetting />)
    const box = await screen.findByRole('checkbox', { name: LABEL })
    await user.click(box)
    expect(await screen.findByRole('alert')).toHaveTextContent('must come from the ScadBuddy UI')
    expect(box).toBeChecked()
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/printer-camera', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<PrinterCameraSetting />)
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('Printer camera')).not.toBeInTheDocument()
  })
})
