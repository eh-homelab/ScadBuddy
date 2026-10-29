import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { LanRunControl } from '../../api/types'
import { server } from '../../mocks/server'
import { LanRunControls } from './LanRunControls'

const RUN = 'fedcba9876543210fedcba9876543210'

function posted(action: 'stop' | 'plate-cleared', status = 202) {
  const calls: string[] = []
  server.use(
    http.post(`/api/v1/print/runs/:id/${action}`, ({ params }) => {
      calls.push(String(params.id))
      return status === 202
        ? HttpResponse.json({ id: RUN }, { status })
        : HttpResponse.json({ title: 'Conflict', detail: 'This run has no prints still to start.' }, { status })
    }),
  )
  return calls
}

function controls(control: Partial<LanRunControl> = {}) {
  render(<LanRunControls control={{ run_id: RUN, waiting: 2, awaiting_clear: false, ...control }} printerName="H2C" />)
}

describe('LanRunControls (#4942, #4939)', () => {
  it('stops the rest only after the confirmation, saying the print on the printer carries on', async () => {
    const calls = posted('stop')
    controls()
    const user = userEvent.setup()
    expect(screen.getByTestId('lan-run-controls')).toHaveTextContent('2 prints still to start')
    expect(screen.queryByRole('button', { name: /Plate is clear/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stop the rest' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('The print on H2C now carries on')
    expect(calls).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Stop them' }))
    await waitFor(() => expect(calls).toEqual([RUN]))
    expect(await screen.findByText(/Stopping the prints still to start/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop the rest' })).toBeDisabled()
  })

  it('keeps them when the confirmation is dismissed', async () => {
    const calls = posted('stop')
    controls()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Stop the rest' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Keep them' }))
    expect(calls).toEqual([])
  })

  it('asks for the plate to be cleared, and starts the next print once it is', async () => {
    const calls = posted('plate-cleared')
    controls({ awaiting_clear: true })
    const user = userEvent.setup()
    expect(screen.getByTestId('lan-run-controls')).toHaveTextContent('Clear the plate of H2C')
    await user.click(screen.getByRole('button', { name: 'Plate is clear — start the next print' }))
    await waitFor(() => expect(calls).toEqual([RUN]))
    expect(await screen.findByText(/Starting the next print/)).toBeInTheDocument()
  })

  it('says why the backend refused', async () => {
    posted('stop', 409)
    controls()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Stop the rest' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Stop them' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no prints still to start')
  })
})
