import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { L } from '../../mocks/features/filament/seed'
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

  it('draws a label for every printer place of ?printers=all, in order, with its code and name', async () => {
    renderPage(<LabelsPage />, { route: '/filament/labels?printers=all' })
    expect(screen.getByRole('heading', { level: 1, name: 'Printer labels' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByTestId('label').length).toBeGreaterThan(0))
    const labels = screen.getAllByTestId('label')
    expect(labels[0]).toHaveTextContent('3DP-31B-598')
    expect(labels.some((label) => label.textContent?.includes('H2C AMS-A slot 1'))).toBe(true)
    for (const label of labels) {
      expect(label.querySelector('svg')).not.toBeNull()
      expect(label.textContent).toMatch(/L-\d+/)
    }
    expect(screen.getAllByTestId('print-label')).toHaveLength(labels.length)
    expect(screen.getByText(`Labels open ${window.location.origin}`)).toBeInTheDocument()
  })

  it('uses the base address of the answer', async () => {
    server.use(http.post('*/api/v1/filament/printer-labels', () => HttpResponse.json({ base_url: 'https://sb.example.com', labels: [{ location_id: 'x', code: 'L-12', name: 'H2C', depth: 0 }], no_report: [] })))
    renderPage(<LabelsPage />, { route: '/filament/labels?printers=p1,p2' })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByText('Labels open https://sb.example.com')).toBeInTheDocument()
  })

  it('draws the label of one place, with the base from the settings', async () => {
    server.use(http.get('*/api/v1/settings', () => HttpResponse.json({ filament_label_base_url: null, public_url: 'https://pub.example.com/' })))
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByTestId('label')).toHaveTextContent('H2C AMS-A slot 1')
    expect(screen.getByText('Labels open https://pub.example.com')).toBeInTheDocument()
  })

  it('names a place that could not be read', async () => {
    renderPage(<LabelsPage />, { route: '/filament/labels?locations=nope' })
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be read')
  })

  it.each([
    ['http://h.example/p/', window.location.origin],
    ['https://h.example/p/', 'https://h.example'],
  ])('reduces the public_url %s to an https origin for one place', async (publicUrl, shown) => {
    server.use(http.get('*/api/v1/settings', () => HttpResponse.json({ filament_label_base_url: '', public_url: publicUrl })))
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByText(`Labels open ${shown}`)).toBeInTheDocument()
  })

  it('draws no label whose payload passes 84 bytes', async () => {
    server.use(http.post('*/api/v1/filament/printer-labels', () => HttpResponse.json({ base_url: `https://${'a'.repeat(70)}.example`, labels: [{ location_id: 'x', code: 'L-12', name: 'H2C', depth: 0 }], no_report: [] })))
    renderPage(<LabelsPage />, { route: '/filament/labels?printers=all' })
    expect(await screen.findByRole('alert')).toHaveTextContent('too long')
    expect(screen.queryAllByTestId('label')).toHaveLength(0)
  })

  it('says no printers, and names no raw id on a failure', async () => {
    server.use(http.post('*/api/v1/filament/printer-labels', () => HttpResponse.json({ base_url: null, labels: [], no_report: [] })))
    const { unmount } = renderPage(<LabelsPage />, { route: '/filament/labels?printers=all' })
    expect(await screen.findByText('No printers.')).toBeInTheDocument()
    unmount()
    server.use(http.post('*/api/v1/filament/printer-labels', () => new HttpResponse(null, { status: 500 })))
    renderPage(<LabelsPage />, { route: '/filament/labels?printers=all' })
    expect(await screen.findByRole('alert')).toHaveTextContent('The printer labels could not be read')
  })

  it('refuses to draw a place label when the settings cannot be read', async () => {
    server.use(http.get('*/api/v1/settings', () => new HttpResponse(null, { status: 500 })))
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    expect(await screen.findByRole('alert')).toHaveTextContent('The label address in Settings could not be read')
    expect(screen.queryAllByTestId('label')).toHaveLength(0)
    expect(screen.queryAllByTestId('print-label')).toHaveLength(0)
    expect(screen.queryByText(/Labels open/)).toBeNull()
    expect(screen.getByRole('button', { name: /Print/ })).toBeDisabled()
  })

  it('names the printers with no report', async () => {
    server.use(
      http.post('*/api/v1/filament/printer-labels', () =>
        HttpResponse.json({ base_url: null, labels: [{ location_id: 'x', code: 'L-12', name: 'H2C', depth: 0 }], no_report: ['H2C'] }),
      ),
    )
    renderPage(<LabelsPage />, { route: '/filament/labels?printers=all' })
    expect(await screen.findByText(/No AMS report for H2C/)).toBeInTheDocument()
    expect(screen.getAllByTestId('label')).toHaveLength(1)
  })

  it('draws a label for each place of ?locations=, in the order given', async () => {
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A4},${L.AMS_A1},${L.AMS_A4}` })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(2))
    expect(screen.getAllByTestId('label').map((label) => label.textContent)).toEqual([expect.stringContaining('slot 4'), expect.stringContaining('slot 1')])
    expect(screen.getAllByTestId('print-label')).toHaveLength(2)
  })

  it('draws the places it could read and names the rest', async () => {
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1},nope` })
    expect(await screen.findByRole('alert')).toHaveTextContent('The label of nope could not be read')
    expect(screen.getAllByTestId('label')).toHaveLength(1)
  })

  it('refuses more than 100 places and reads none', async () => {
    let reads = 0
    server.use(
      http.get('*/api/v1/filament/locations/:id', () => {
        reads++
        return new HttpResponse(null, { status: 404 })
      }),
    )
    const ids = Array.from({ length: 101 }, (_, i) => `p${i}`).join(',')
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${ids}` })
    expect(await screen.findByText('Print at most 100 labels at a time')).toBeInTheDocument()
    expect(screen.queryAllByTestId('label')).toHaveLength(0)
    expect(reads).toBe(0)
  })

  it('refuses more than 100 printer labels and draws none', async () => {
    const labels = Array.from({ length: 101 }, (_, i) => ({ location_id: `x${i}`, code: `L-${i + 1}`, name: `P${i}`, depth: 0 }))
    server.use(http.post('*/api/v1/filament/printer-labels', () => HttpResponse.json({ base_url: null, labels, no_report: [] })))
    renderPage(<LabelsPage />, { route: '/filament/labels?printers=all' })
    expect(await screen.findByText('Print at most 100 labels at a time')).toBeInTheDocument()
    expect(screen.queryAllByTestId('label')).toHaveLength(0)
    expect(screen.queryByText('No printers.')).toBeNull()
    expect(screen.getByRole('button', { name: /Print/ })).toBeDisabled()
  })

  it('goes back to the Locations tab from a place label', () => {
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    expect(screen.getByRole('link', { name: /Locations/ })).toHaveAttribute('href', '/filament/locations')
  })
})
