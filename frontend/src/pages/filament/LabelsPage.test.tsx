import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { LabelsPage } from './LabelsPage'

const detail = (id: string, number: number) => ({
  id,
  number,
  colour_name: 'Red',
  effective: { colour_name: 'Red' },
  names: { material: 'PLA', brand: 'Bambu Lab' },
})

function serve() {
  server.use(http.get('*/api/v1/filament/spools/:id', ({ params }) => HttpResponse.json(detail(String(params.id), params.id === 'a' ? 1 : 2))))
}

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('the labels page', () => {
  it('loads each spool of the address and draws a label for it', async () => {
    serve()
    renderPage(<LabelsPage />, { route: '/filament/labels?ids=a,b' })
    expect(screen.getByRole('heading', { level: 1, name: 'Spool labels' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(2))
    expect(screen.getAllByTestId('label').map((label) => label.textContent)).toEqual([expect.stringContaining('#1'), expect.stringContaining('#2')])
    // The print copy, a child of <body> that the print rules show alone.
    expect(screen.getAllByTestId('print-label')).toHaveLength(2)
    expect(document.getElementById('label-print-root')?.parentElement).toBe(document.body)
  })

  it('reads at most six spools at the same time', async () => {
    let open = 0
    let most = 0
    server.use(
      http.get('*/api/v1/filament/spools/:id', async ({ params }) => {
        open++
        most = Math.max(most, open)
        await new Promise((resolve) => setTimeout(resolve, 5))
        open--
        return HttpResponse.json(detail(String(params.id), 1))
      }),
    )
    const ids = Array.from({ length: 20 }, (_, i) => `s${i}`).join(',')
    renderPage(<LabelsPage />, { route: `/filament/labels?ids=${ids}` })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(20))
    expect(most).toBeLessThanOrEqual(6)
  })

  it('refuses more than 100 labels and draws none', () => {
    const ids = Array.from({ length: 101 }, (_, i) => `s${i}`).join(',')
    renderPage(<LabelsPage />, { route: `/filament/labels?ids=${ids}` })
    expect(screen.getByText('Print at most 100 labels at a time')).toBeInTheDocument()
    expect(screen.queryAllByTestId('label')).toHaveLength(0)
  })

  it('names a spool that could not be read', async () => {
    server.use(http.get('*/api/v1/filament/spools/:id', () => new HttpResponse(null, { status: 404 })))
    renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    expect(await screen.findByRole('alert')).toHaveTextContent('One spool could not be read')
  })

  it('shows the roll layout when storage is blocked', async () => {
    serve()
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    expect(screen.getByRole('radio', { name: 'Roll' })).toBeChecked()
    await user.click(screen.getByRole('radio', { name: 'Sheet' }))
    expect(screen.getByRole('radio', { name: 'Sheet' })).toBeChecked()
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
  })

  it('remembers the sheet layout', async () => {
    serve()
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await user.click(screen.getByRole('radio', { name: 'Sheet' }))
    expect(localStorage.getItem('scadbuddy.labels.layout')).toBe('sheet')
  })

  it('prints through the browser', async () => {
    serve()
    const print = vi.spyOn(window, 'print').mockImplementation(() => {})
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Print' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Print' }))
    expect(print).toHaveBeenCalled()
  })
})
