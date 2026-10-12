import { render, screen, fireEvent } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { server } from '../../mocks/server'
import { PreviewFailure } from './PlateScene'

// #2111 — the error fallback of the 3D preview. Errors are shaped as useLoader wraps
// three's FileLoader HttpError.
const URL = '/api/v1/print/library/7/preview.glb?plate=1'
const loaderError = (status: number, text: string) =>
  new Error(`Could not load ${URL}: fetch for "http://localhost${URL}" responded with ${status}: ${text}`)

describe('PlateScene · PreviewFailure (#2111)', () => {
  it("shows the backend's reason for a refusal, and no Try again", async () => {
    server.use(
      http.get('/api/v1/print/library/7/preview.glb', () =>
        HttpResponse.json(
          { title: 'Unprocessable Content', status: 422, detail: 'lid.gcode.3mf: it is sliced already, so ScadBuddy cannot read its mesh' },
          { status: 422 },
        ),
      ),
    )
    render(<PreviewFailure url={URL} error={loaderError(422, 'Unprocessable Content')} retry={vi.fn()} />)
    expect(
      await screen.findByText(
        "The plate's preview cannot be shown: lid.gcode.3mf: it is sliced already, so ScadBuddy cannot read its mesh",
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('offers Try again for a failure that may pass', () => {
    const retry = vi.fn()
    render(<PreviewFailure url={URL} error={loaderError(503, 'Service Unavailable')} retry={retry} />)
    expect(screen.getByRole('alert')).toHaveTextContent("The plate's preview could not be loaded.")
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(retry).toHaveBeenCalled()
  })

  it('offers Try again when the request got no answer at all', () => {
    render(<PreviewFailure url={URL} error={new Error(`Could not load ${URL}: Failed to fetch`)} retry={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })
})
