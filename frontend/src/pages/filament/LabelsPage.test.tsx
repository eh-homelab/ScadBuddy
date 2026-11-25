import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type * as embed from '../../lib/embed'
import { downloadBlob } from '../../lib/embed'
import { L } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { LabelsPage } from './LabelsPage'

// Loads the file the way the real one does; saving it is lib/embed's own test.
const saved: Blob[] = []
vi.mock('../../lib/embed', async (original) => ({
  ...(await original<typeof embed>()),
  downloadBlob: vi.fn(async (load: () => Promise<Blob>) => {
    saved.push(await load())
  }),
}))

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
  vi.mocked(downloadBlob).mockClear()
  saved.length = 0
  localStorage.clear()
})

/** The settings with the label template `choice`, and every `PUT /settings` body in `puts`. */
function serveTemplate(choice: object, puts: unknown[] = []) {
  server.use(
    http.get('*/api/v1/settings', () => HttpResponse.json({ filament_label_template: choice, filament_label_base_url: 'https://sb.example.com', public_url: null })),
    http.put('*/api/v1/settings', async ({ request }) => {
      puts.push(await request.json())
      return HttpResponse.json({})
    }),
  )
  return puts
}

describe('the label template (#7157)', () => {
  it('draws the labels at the stored template', async () => {
    serve()
    serveTemplate({ template: 'box_62x29', width_mm: null, height_mm: null })
    renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 62mm; height: 29mm/))
    expect(screen.getByTestId('print-label').getAttribute('style')).toMatch(/width: 62mm; height: 29mm/)
    expect(await screen.findByText('1 of 1 ready, 62 x 29 mm each.')).toBeInTheDocument()
  })

  it('saves the template chosen, for place labels too', async () => {
    const puts = serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    const { user } = renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    await choose(user, await screen.findByRole('combobox', { name: 'Label' }), 'AMS holder 75 x 55 mm')
    await waitFor(() => expect(puts).toEqual([{ filament_label_template: { template: 'ams_holder_75x55' } }]))
    expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 75mm; height: 55mm/)
  })

  it('keeps a pick made before the stored template arrives', async () => {
    serve()
    const puts = serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    let answer: () => void = () => {}
    const held = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.get('*/api/v1/settings', async () => {
        await held
        return HttpResponse.json({ filament_label_template: { template: 'box_62x29', width_mm: null, height_mm: null }, filament_label_base_url: null, public_url: null })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await choose(user, await screen.findByRole('combobox', { name: 'Label' }), 'Box label 40 x 30 mm')
    await waitFor(() => expect(puts).toHaveLength(1))
    answer()
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 40mm; height: 30mm/)
    expect(puts).toEqual([{ filament_label_template: { template: 'box_40x30' } }])
  })

  it('shows the stored template under an unfinished custom size, and keeps the fields open', async () => {
    serve()
    const puts = serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    let answer: () => void = () => {}
    const held = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.get('*/api/v1/settings', async () => {
        await held
        return HttpResponse.json({ filament_label_template: { template: 'custom', width_mm: 90, height_mm: 45 }, filament_label_base_url: null, public_url: null })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await choose(user, await screen.findByRole('combobox', { name: 'Label' }), 'Custom size')
    const width = screen.getByRole('spinbutton', { name: 'Width (mm)' })
    await user.clear(width)
    await user.type(width, '7')
    answer()
    await waitFor(() => expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 90mm; height: 45mm/))
    // The fields stay open with what the person typed; nothing was saved.
    expect(screen.getByRole('spinbutton', { name: 'Width (mm)' })).toHaveValue(7)
    expect(puts).toEqual([])
  })

  it('sends only the newest of quick picks, after the one in flight (R-7157-9)', async () => {
    serve()
    serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    const puts: unknown[] = []
    let answer: () => void = () => {}
    const held = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.put('*/api/v1/settings', async ({ request }) => {
        puts.push(await request.json())
        if (puts.length === 1) await held
        return HttpResponse.json({})
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    const picker = await screen.findByRole('combobox', { name: 'Label' })
    await choose(user, picker, 'Box label 40 x 30 mm')
    await waitFor(() => expect(puts).toHaveLength(1))
    await choose(user, picker, 'Box label 62 x 29 mm')
    await choose(user, picker, 'AMS holder 75 x 55 mm')
    expect(puts).toHaveLength(1)
    answer()
    await waitFor(() => expect(puts).toHaveLength(2))
    expect(puts[1]).toEqual({ filament_label_template: { template: 'ams_holder_75x55' } })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(puts).toHaveLength(2)
    expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 75mm; height: 55mm/)
  })

  it('does not save a custom size when another template is picked from its field', async () => {
    serve()
    const puts = serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    const picker = await screen.findByRole('combobox', { name: 'Label' })
    await choose(user, picker, 'Custom size')
    const width = screen.getByRole('spinbutton', { name: 'Width (mm)' })
    await user.clear(width)
    await user.type(width, '80')
    // The field is still focused: the picker's click blurs it.
    await choose(user, picker, 'Box label 40 x 30 mm')
    await waitFor(() => expect(puts).toEqual([{ filament_label_template: { template: 'box_40x30' } }]))
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(puts).toHaveLength(1)
    expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 40mm; height: 30mm/)
  })

  it('saves a custom size once both lengths are in bounds', async () => {
    serve()
    const puts = serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await choose(user, await screen.findByRole('combobox', { name: 'Label' }), 'Custom size')
    const width = screen.getByRole('spinbutton', { name: 'Width (mm)' })
    const height = screen.getByRole('spinbutton', { name: 'Height (mm)' })
    await user.clear(width)
    await user.type(width, '30')
    await user.tab()
    expect(await screen.findByText('A custom label is 40 to 190 mm wide and 25 to 190 mm tall.')).toBeInTheDocument()
    expect(puts).toEqual([])
    await user.clear(width)
    await user.type(width, '80')
    await user.clear(height)
    await user.type(height, '40')
    await user.tab()
    await waitFor(() => expect(puts.at(-1)).toEqual({ filament_label_template: { template: 'custom', width_mm: 80, height_mm: 40 } }))
    await waitFor(() => expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 80mm; height: 40mm/))
  })

  it('prints a sheet template on its own grid, a page per sheet, with no layout choice', async () => {
    server.use(http.get('*/api/v1/filament/spools/:id', ({ params }) => HttpResponse.json(detail(String(params.id), 1))))
    serveTemplate({ template: 'avery_l7160', width_mm: null, height_mm: null })
    const ids = Array.from({ length: 22 }, (_, i) => `s${i}`).join(',')
    renderPage(<LabelsPage />, { route: `/filament/labels?ids=${ids}` })
    await waitFor(() => expect(screen.getAllByTestId('print-label')).toHaveLength(22))
    // 21 a sheet: two pages.
    expect(document.querySelectorAll('#label-print-root > [data-sheet-page]')).toHaveLength(2)
    expect(screen.queryByRole('radio', { name: 'Sheet' })).toBeNull()
    expect(document.querySelector('style')?.textContent).toContain('size: 210mm 297mm')
  })

  it('downloads the PDF of the labels drawn, at the template shown', async () => {
    serve()
    serveTemplate({ template: 'box_40x30', width_mm: null, height_mm: null })
    let body: unknown = null
    server.use(
      http.post('*/api/v1/filament/labels', async ({ request }) => {
        body = await request.json()
        return new HttpResponse(new Blob(['%PDF-1.4'], { type: 'application/pdf' }), { headers: { 'Content-Type': 'application/pdf' } })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a,b' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await waitFor(() => expect(screen.getAllByTestId('label')[0]!.getAttribute('style')).toMatch(/width: 40mm/))
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(savedName()).toBe('labels.pdf')
    expect(body).toEqual({ spool_ids: ['a', 'b'], location_ids: [], template: { template: 'box_40x30' }, layout: 'roll', format: 'pdf', dpi: 300 })
  })

  it('says why the PDF was refused', async () => {
    serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    server.use(
      http.post('*/api/v1/filament/labels', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'A place label needs an address: set the label address in Settings, or the public URL.', code: 'no_label_base' }, { status: 409 }),
      ),
    )
    const { user } = renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Download' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('A place label needs an address')
  })

  it('keeps the Download button disabled while the places load', async () => {
    let release = () => {}
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.post('*/api/v1/filament/printer-labels', async () => {
        await gate
        return HttpResponse.json({ base_url: 'https://sb.example', labels: [{ location_id: 'x', code: 'L-12', name: 'H2C', depth: 0 }], no_report: [] })
      }),
    )
    renderPage(<LabelsPage />, { route: '/filament/labels?printers=all' })
    expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled()
    expect(screen.getByText('Loading labels…')).toBeInTheDocument()
    release()
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled()
  })

  it('holds the download of place labels drawn at the page’s own address, and says why', async () => {
    const noBase = () => HttpResponse.json({ filament_label_template: { template: 'roll_50x25', width_mm: null, height_mm: null }, filament_label_base_url: null, public_url: null })
    server.use(http.get('*/api/v1/settings', noBase))
    const { unmount } = renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    const button = screen.getByRole('button', { name: 'Download' })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription('A download of place labels needs a label address. Set one in Settings, or an https public URL.')
    unmount()
    // The same for the printers' places, when `printerLabels` answers no address.
    server.use(http.post('*/api/v1/filament/printer-labels', () => HttpResponse.json({ base_url: null, labels: [{ location_id: 'x', code: 'L-12', name: 'H2C', depth: 0 }], no_report: [] })))
    const printers = renderPage(<LabelsPage />, { route: '/filament/labels?printers=all' })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled()
    printers.unmount()
    // Spool labels need no address: their PDF stays on offer.
    serve()
    renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    expect(screen.queryByText(/needs a label address/)).toBeNull()
  })
})

const FIELDS = ['number', 'material', 'colour', 'brand', 'qr']

/** The settings with the label output `output`, and every `PUT /settings` body in `puts`. */
function serveOutput(output: object, puts: unknown[] = []) {
  server.use(
    http.get('*/api/v1/settings', () =>
      HttpResponse.json({
        filament_label_template: { template: 'roll_50x25', width_mm: null, height_mm: null },
        filament_label_output: output,
        filament_label_base_url: 'https://sb.example.com',
        public_url: null,
      }),
    ),
    http.put('*/api/v1/settings', async ({ request }) => {
      puts.push(await request.json())
      return HttpResponse.json({})
    }),
  )
  return puts
}

/** `POST /filament/labels` answers `type`; each body sent goes in the list returned. */
function serveLabels(type: string) {
  const bodies: unknown[] = []
  server.use(
    http.post('*/api/v1/filament/labels', async ({ request }) => {
      bodies.push(await request.json())
      return new HttpResponse(new Blob(['x'], { type }), { headers: { 'Content-Type': type } })
    }),
  )
  return bodies
}

/** The file name the page asked `downloadBlob` to save under. */
function savedName() {
  const name = vi.mocked(downloadBlob).mock.calls[0]![1]
  return typeof name === 'function' ? name() : name
}

describe('the label output (#7216)', () => {
  // R-7216-17: a pick never writes fields the page has not read.
  it('saves a pick made before the settings arrive once, with the stored fields', async () => {
    serve()
    const stored = ['number', 'qr', 'weight']
    const puts = serveOutput({ format: 'pdf', dpi: 300, fields: stored })
    let answer: () => void = () => {}
    const held = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.get('*/api/v1/settings', async () => {
        await held
        return HttpResponse.json({ filament_label_output: { format: 'pdf', dpi: 300, fields: stored }, filament_label_base_url: null, public_url: null })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await user.click(await screen.findByRole('radio', { name: 'PNG' }))
    expect(screen.getByRole('radio', { name: 'PNG' })).toBeChecked()
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(puts).toEqual([])
    answer()
    await waitFor(() => expect(puts).toEqual([{ filament_label_output: { format: 'png', dpi: 300, fields: stored } }]))
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(puts).toHaveLength(1)
  })

  it('keeps the format and dpi picked over a stored output read after the pick', async () => {
    serve()
    const puts = serveOutput({ format: 'pdf', dpi: 300, fields: FIELDS })
    let answer: () => void = () => {}
    const held = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.get('*/api/v1/settings', async () => {
        await held
        return HttpResponse.json({ filament_label_output: { format: 'png', dpi: 203, fields: FIELDS }, filament_label_base_url: null, public_url: null })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await user.click(await screen.findByRole('radio', { name: 'PNG' }))
    await choose(user, await screen.findByRole('combobox', { name: 'Resolution' }), '600 dpi')
    answer()
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts).toEqual([{ filament_label_output: { format: 'png', dpi: 600, fields: FIELDS } }])
    expect(screen.getByRole('radio', { name: 'PNG' })).toBeChecked()
    expect(screen.getByRole('combobox', { name: 'Resolution' })).toHaveTextContent('600 dpi')
  })

  it('saves no output when the settings cannot be read, and downloads at the pick', async () => {
    serve()
    const puts = serveOutput({ format: 'pdf', dpi: 300, fields: FIELDS })
    server.use(http.get('*/api/v1/settings', () => HttpResponse.json({ detail: 'no' }, { status: 500 })))
    const bodies = serveLabels('image/png')
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(screen.getByRole('radio', { name: 'PNG' }))
    expect(await screen.findByText('The file choice could not be saved. It holds on this page only.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ format: 'png', dpi: 300 })
    expect(puts).toEqual([])
  })

  it('downloads a PNG when the output is PNG', async () => {
    serve()
    const puts = serveOutput({ format: 'pdf', dpi: 300, fields: FIELDS })
    const bodies = serveLabels('image/png')
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(await screen.findByRole('radio', { name: 'PNG' }))
    await choose(user, await screen.findByRole('combobox', { name: 'Resolution' }), '600 dpi')
    const png600 = { filament_label_output: { format: 'png', dpi: 600, fields: FIELDS } }
    await waitFor(() => expect(puts.at(-1)).toEqual(png600))
    expect(puts.filter((one) => JSON.stringify(one) === JSON.stringify(png600))).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Download' })).toHaveAttribute('title', 'Download PNG images at 600 dpi')
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ spool_ids: ['a'], format: 'png', dpi: 600 })
    expect(savedName()).toBe('labels.png')
  })

  it('names a ZIP answer labels.zip', async () => {
    serve()
    serveOutput({ format: 'png', dpi: 300, fields: FIELDS })
    serveLabels('application/zip')
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a,b' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(savedName()).toBe('labels.zip')
  })

  it('shows the stored output choice', async () => {
    serve()
    const puts = serveOutput({ format: 'png', dpi: 203, fields: FIELDS })
    renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('radio', { name: 'PNG' })).toBeChecked())
    expect(screen.getByRole('combobox', { name: 'Resolution' })).toHaveTextContent('203 dpi')
    expect(screen.getByRole('button', { name: 'Download' })).toHaveAttribute('title', 'Download PNG images at 203 dpi')
    expect(puts).toEqual([])
  })

  it('hides the dpi for PDF', async () => {
    serve()
    const puts = serveOutput({ format: 'png', dpi: 600, fields: FIELDS })
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Resolution' })).toBeInTheDocument())
    await user.click(screen.getByRole('radio', { name: 'PDF' }))
    expect(screen.queryByRole('combobox', { name: 'Resolution' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Download' })).toHaveAttribute('title', 'Download a PDF')
    await waitFor(() => expect(puts).toEqual([{ filament_label_output: { format: 'pdf', dpi: 600, fields: FIELDS } }]))
  })
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
