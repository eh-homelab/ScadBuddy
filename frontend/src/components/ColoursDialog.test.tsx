import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import { ColoursDialog } from './ColoursDialog'

const JOB = 'c'.repeat(32)

/** The route as the backend answers it: the grid, and its legend in the headers. */
function answer(colours: string[], columns: number, seen: string[] = []) {
  server.use(
    http.get(`/api/v1/jobs/${JOB}/colours.png`, ({ request }) => {
      seen.push(new URL(request.url).searchParams.get('view') ?? '')
      return new HttpResponse(new Uint8Array([137, 80, 78, 71]), {
        headers: {
          'Content-Type': 'image/png',
          'X-ScadBuddy-Colours': colours.join(','),
          'X-ScadBuddy-Colour-Columns': String(columns),
        },
      })
    }),
  )
}

describe('ColoursDialog', () => {
  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => 'blob:colours')
    URL.revokeObjectURL = vi.fn()
  })

  it('shows the grid with each tile named by its extruder, in the order the route gives', async () => {
    answer(['#00FF00', '#FF0000', '#123456'], 2)
    render(<ColoursDialog open jobId={JOB} colors={['#ff0000', '#00ff00']} onClose={() => undefined} />)

    const legend = await screen.findByRole('list', { name: 'Tiles, row by row' })
    expect(within(legend).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Tile 1: Extruder 2, #00FF00',
      'Tile 2: Extruder 1, #FF0000',
      // A colour the parts carry that is not one of the job's extruders.
      'Tile 3: Not an extruder colour, #123456',
    ])
    expect(screen.getByRole('dialog', { name: 'Colours' }).querySelector('img')).toHaveAttribute('src', 'blob:colours')
  })

  it('draws again from another view', async () => {
    const seen: string[] = []
    answer(['#FF0000', '#00FF00'], 2, seen)
    const user = userEvent.setup()
    render(<ColoursDialog open jobId={JOB} colors={['#FF0000', '#00FF00']} onClose={() => undefined} />)
    await screen.findByRole('list', { name: 'Tiles, row by row' })

    await user.selectOptions(screen.getByRole('combobox', { name: 'View' }), 'top')
    await vi.waitFor(() => expect(seen).toEqual(['iso', 'top']))
    expect(await screen.findByRole('list', { name: 'Tiles, row by row' })).toBeInTheDocument()
  })

  it("says why when the breakdown cannot be drawn", async () => {
    server.use(
      http.get(`/api/v1/jobs/${JOB}/colours.png`, () =>
        HttpResponse.json(
          { title: 'Unprocessable Content', status: 422, detail: '17 colours is more than a breakdown draws (16)' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    render(<ColoursDialog open jobId={JOB} colors={['#FF0000', '#00FF00']} onClose={() => undefined} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('17 colours is more than a breakdown draws (16)')
  })

  it('shows the grid once a view that failed draws after all', async () => {
    let calls = 0
    server.use(
      http.get(`/api/v1/jobs/${JOB}/colours.png`, () => {
        calls += 1
        if (calls === 1) {
          return HttpResponse.json(
            { title: 'Service Unavailable', status: 503, detail: 'The breakdown took too long to draw' },
            { status: 503, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        return new HttpResponse(new Uint8Array([137, 80, 78, 71]), {
          headers: { 'Content-Type': 'image/png', 'X-ScadBuddy-Colours': '#FF0000,#00FF00', 'X-ScadBuddy-Colour-Columns': '2' },
        })
      }),
    )
    const props = { jobId: JOB, colors: ['#FF0000', '#00FF00'], onClose: () => undefined }
    const { rerender } = render(<ColoursDialog open {...props} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('took too long')

    // Closed and opened again: the same view is asked for again, and this time it draws.
    rerender(<ColoursDialog open={false} {...props} />)
    rerender(<ColoursDialog open {...props} />)
    expect(await screen.findByRole('list', { name: 'Tiles, row by row' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('asks for nothing while closed', () => {
    const seen: string[] = []
    answer(['#FF0000', '#00FF00'], 2, seen)
    render(<ColoursDialog open={false} jobId={JOB} colors={['#FF0000', '#00FF00']} onClose={() => undefined} />)
    expect(seen).toEqual([])
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
