import { act, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { listTypes, upsertType, type SpoolTypeCreate } from '../../api/filament'
import { inventoryApi } from '../inventory/api'
import type * as embed from '../../lib/embed'
import { downloadBlob } from '../../lib/embed'
import { LABEL_TEMPLATES } from '../../mocks/features/filament'
import { B, L, M } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { printTiming } from '../../components/filament/labelSheet'
import { LabelsPage, READ_AT_ONCE } from './LabelsPage'

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

/**
 * Every `GET /filament/label-templates` answer the page reads, as the promise of its JSON body. `settled(n)` waits
 * until the page has read `n` answers' bodies, then one macrotask, which runs after the rest of each read's chain
 * (microtasks only): a test's assert then sees what the page made of every answer, however slow the runner.
 * `calls` lists every request the page starts, `<method> <url>`, in the order it starts them: the client calls
 * `fetch` before its first `await`, so a request the page made is here before any later one reaches a handler.
 */
function watchTemplateReads() {
  const reads: Promise<unknown>[] = []
  const calls: string[] = []
  const real = globalThis.fetch
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    calls.push(`${init?.method ?? 'GET'} ${url}`)
    const response = await real(input, init)
    if (url.includes('/filament/label-templates')) {
      const json = response.json.bind(response)
      response.json = () => {
        const body = json()
        reads.push(body.catch(() => {}))
        return body
      }
    }
    return response
  })
  return {
    calls,
    async settled(count: number) {
      await waitFor(() => expect(reads.length).toBeGreaterThanOrEqual(count))
      await Promise.all(reads)
      await act(() => new Promise((resolve) => setTimeout(resolve, 0)))
    },
  }
}

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
    expect(body).toEqual({ spool_ids: ['a', 'b'], location_ids: [], type_ids: [], item_ids: [], kind_ids: [], template: { template: 'box_40x30' }, layout: 'roll', format: 'pdf', dpi: 300, fields: ['number', 'material', 'colour', 'brand', 'qr'] })
  })

  it('says why the PDF was refused', async () => {
    serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    server.use(
      http.post('*/api/v1/filament/labels', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'A code label needs an address: set the label address in Settings, or the public URL.', code: 'no_label_base' }, { status: 409 }),
      ),
    )
    const { user } = renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Download' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('A code label needs an address')
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
    expect(button).toHaveAccessibleDescription('A download of code labels needs a label address. Set one in Settings, or an https public URL.')
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

describe('spool type, item and kind labels (#7169)', () => {
  const serveTypes = () =>
    server.use(
      http.get('*/api/v1/filament/types/:id', ({ params }) =>
        params.id === 'b'
          ? HttpResponse.json({ type: 'about:blank', title: 'Not Found', status: 404, detail: 'no type b' }, { status: 404 })
          : HttpResponse.json({ id: String(params.id), number: 4, code: '10101', colour_name: 'Charcoal' }),
      ),
    )
  const captureDownload = () => {
    const bodies: Record<string, unknown>[] = []
    server.use(
      http.post('*/api/v1/filament/labels', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>)
        return new HttpResponse(new Blob(['%PDF-1.4'], { type: 'application/pdf' }), { headers: { 'Content-Type': 'application/pdf' } })
      }),
    )
    return bodies
  }

  it('draws a spool type label from ?types=', async () => {
    serveTypes()
    const bodies = captureDownload()
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?types=a' })
    expect(screen.getByRole('heading', { name: 'Spool type labels' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByTestId('label')).toHaveTextContent('T-4')
    expect(screen.getByTestId('label')).toHaveTextContent('10101 Charcoal')
    expect(screen.getByRole('link', { name: /Types/ })).toHaveAttribute('href', '/filament/types')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ type_ids: ['a'], spool_ids: [], location_ids: [], item_ids: [], kind_ids: [] })
  })

  it('draws item and kind labels from ?items= and ?kinds=', async () => {
    server.use(
      http.get('*/api/v1/inventory/items/:id', ({ params }) => HttpResponse.json({ id: String(params.id), number: 9, code: 'I-9', name: 'M3 nut' })),
      http.get('*/api/v1/inventory/kinds', () => HttpResponse.json({ kinds: [{ id: 'hardware', number: 3, code: 'K-3', name: 'Hardware' }] })),
    )
    const bodies = captureDownload()
    const items = renderPage(<LabelsPage />, { route: '/filament/labels?items=x' })
    expect(screen.getByRole('heading', { name: 'Item labels' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByTestId('label')).toHaveTextContent('I-9')
    expect(screen.getByTestId('label')).toHaveTextContent('M3 nut')
    expect(screen.getByRole('link', { name: /Inventory/ })).toHaveAttribute('href', '/inventory')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await items.user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ item_ids: ['x'], kind_ids: [], type_ids: [] })
    items.unmount()

    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?kinds=hardware' })
    expect(screen.getByRole('heading', { name: 'Kind labels' })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByTestId('label')).toHaveTextContent('K-3')
    expect(screen.getByRole('link', { name: /Kinds/ })).toHaveAttribute('href', '/inventory/kinds')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(bodies).toHaveLength(2))
    expect(bodies[1]).toMatchObject({ kind_ids: ['hardware'], item_ids: [], type_ids: [] })
  })

  it('numbers a new type and a new item one past the highest of both, and the mock label shows it', async () => {
    const types = (await listTypes({ includeArchived: true })).map((type) => type.number)
    const items = (await inventoryApi.items({ archived: true })).map((item) => item.number)
    const made = await upsertType({ code: 'NEW-PLA-RED', material_id: M.PLA, brand_id: B.BAMBU, rgba: '#ff0000ff', label_weight_g: 1000 } as SpoolTypeCreate)
    expect(made.number).toBe(Math.max(...types, ...items) + 1)
    const first = await inventoryApi.createItem({ kind_id: 'hardware', name: 'M3 nut' })
    const second = await inventoryApi.createItem({ kind_id: 'hardware', name: 'M3 bolt' })
    expect(first.number).toBe(made.number + 1)
    expect(second.number).toBe(first.number + 1)
    renderPage(<LabelsPage />, { route: `/filament/labels?types=${made.id}` })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByTestId('label')).toHaveTextContent(`T-${made.number}`)
  })

  it('names a type that could not be read and draws the rest', async () => {
    serveTypes()
    renderPage(<LabelsPage />, { route: '/filament/labels?types=a,b' })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByText(/The label of b could not be read/)).toBeInTheDocument()
  })

  const serveKinds = () =>
    server.use(http.get('*/api/v1/inventory/kinds', () => HttpResponse.json({ kinds: [{ id: 'hardware', number: 3, code: 'K-3', name: 'Hardware' }] })))
  const serveItems = () =>
    server.use(
      http.get('*/api/v1/inventory/items/:id', ({ params }) =>
        params.id === 'bad'
          ? HttpResponse.json({ type: 'about:blank', title: 'Not Found', status: 404, detail: 'no item bad' }, { status: 404 })
          : HttpResponse.json({ id: String(params.id), number: 9, code: 'I-9', name: 'M3 nut' }),
      ),
    )

  it('names an item that could not be read and draws the rest', async () => {
    serveItems()
    renderPage(<LabelsPage />, { route: '/filament/labels?items=x,bad' })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByText(/The label of bad could not be read/)).toBeInTheDocument()
  })

  it('names a kind that is not in the list and draws the rest', async () => {
    serveKinds()
    renderPage(<LabelsPage />, { route: '/filament/labels?kinds=hardware,gone' })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByText(/The label of gone could not be read/)).toBeInTheDocument()
  })

  it('names each kind as unreadable when the kinds list fails, and draws the other subjects', async () => {
    serveTypes()
    server.use(http.get('*/api/v1/inventory/kinds', () => HttpResponse.json({ type: 'about:blank', title: 'Server Error', status: 500 }, { status: 500 })))
    renderPage(<LabelsPage />, { route: '/filament/labels?types=a&kinds=hardware,gone' })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByTestId('label')).toHaveTextContent('T-4')
    expect(screen.getByText(/The labels of hardware, gone could not be read/)).toBeInTheDocument()
  })

  it('draws types and items together and sends both id lists', async () => {
    serveTypes()
    serveItems()
    const bodies = captureDownload()
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?types=a&items=x' })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(2))
    expect(screen.getAllByTestId('label')[0]).toHaveTextContent('T-4')
    expect(screen.getAllByTestId('label')[1]).toHaveTextContent('I-9')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ type_ids: ['a'], item_ids: ['x'], kind_ids: [], location_ids: [] })
  })

  it('keeps at most READ_AT_ONCE reads open across places, types and items together', async () => {
    const held: (() => void)[] = []
    let open = 0
    let most = 0
    const hold = async (body: Record<string, unknown>) => {
      open += 1
      most = Math.max(most, open)
      await new Promise<void>((release) => held.push(release))
      open -= 1
      return HttpResponse.json(body)
    }
    server.use(
      http.get('*/api/v1/filament/locations/:id', ({ params }) => hold({ id: String(params.id), number: 1, code: `L-${String(params.id)}`, name: 'Shelf' })),
      http.get('*/api/v1/filament/types/:id', ({ params }) => hold({ id: String(params.id), number: 4, code: '10101', colour_name: 'Charcoal' })),
      http.get('*/api/v1/inventory/items/:id', ({ params }) => hold({ id: String(params.id), number: 9, code: 'I-9', name: 'M3 nut' })),
    )
    const ids = (prefix: string) => Array.from({ length: 10 }, (_, at) => `${prefix}${at}`).join(',')
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${ids('l')}&types=${ids('t')}&items=${ids('i')}` })
    let answered = 0
    while (answered < 30) {
      // Wait until the pool is full (or holds the rest), so the count does not hang on the runner's speed.
      await waitFor(() => expect(held.length).toBe(Math.min(READ_AT_ONCE, 30 - answered)))
      const now = held.splice(0)
      answered += now.length
      now.forEach((release) => release())
    }
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(30))
    expect(most).toBe(READ_AT_ONCE)
  })

  it('says a type label needs the label address, as the place labels do', async () => {
    serveTypes()
    server.use(
      http.get('*/api/v1/settings', () => HttpResponse.json({ filament_label_template: { template: 'roll_50x25', width_mm: null, height_mm: null }, filament_label_base_url: null, public_url: null })),
    )
    renderPage(<LabelsPage />, { route: '/filament/labels?types=a' })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    const button = screen.getByRole('button', { name: 'Download' })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription('A download of code labels needs a label address. Set one in Settings, or an https public URL.')
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

  it('saves the fields chosen', async () => {
    serve()
    const puts = serveOutput({ format: 'png', dpi: 203, fields: FIELDS })
    const bodies = serveLabels('image/png')
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('radio', { name: 'PNG' })).toBeChecked())
    expect(screen.getAllByText('Bambu Lab')).toHaveLength(2)
    await user.click(screen.getByRole('checkbox', { name: 'Brand' }))
    await waitFor(() => expect(puts).toEqual([{ filament_label_output: { format: 'png', dpi: 203, fields: ['number', 'material', 'colour', 'qr'] } }]))
    expect(screen.queryByText('Bambu Lab')).toBeNull()
    expect(screen.getByRole('checkbox', { name: 'Brand' })).not.toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ format: 'png', dpi: 203, fields: ['number', 'material', 'colour', 'qr'] })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(puts).toHaveLength(1)
  })

  // R-7216-17, R-7216-18: a field tick changes one box of the stored fields, and a format pick keeps the stored dpi.
  it('saves a field tick and a format pick made before the settings arrive once, over the stored rest', async () => {
    serve()
    const puts = serveOutput({ format: 'pdf', dpi: 300, fields: FIELDS })
    let answer: () => void = () => {}
    const held = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.get('*/api/v1/settings', async () => {
        await held
        return HttpResponse.json({ filament_label_output: { format: 'pdf', dpi: 600, fields: ['number', 'weight', 'qr'] }, filament_label_base_url: null, public_url: null })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await user.click(await screen.findByRole('checkbox', { name: 'Colour code' }))
    await user.click(screen.getByRole('radio', { name: 'PNG' }))
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(puts).toEqual([])
    answer()
    // The stored fields with the one box ticked; the dpi is the stored one.
    await waitFor(() => expect(puts).toEqual([{ filament_label_output: { format: 'png', dpi: 600, fields: ['number', 'hex', 'weight', 'qr'] } }]))
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(puts).toHaveLength(1)
    expect(screen.getByRole('checkbox', { name: 'Colour code' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Weight' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Brand' })).not.toBeChecked()
  })

  it('saves no field tick when the settings cannot be read, and downloads the fields shown', async () => {
    serve()
    const puts = serveOutput({ format: 'pdf', dpi: 300, fields: FIELDS })
    // The read fails before the tick, so the tick takes the failed-read path, not the waiting one.
    const failed = Promise.reject(new Error('no'))
    failed.catch(() => {})
    const read = vi.spyOn(api, 'getSettings').mockReturnValue(failed)
    const bodies = serveLabels('application/pdf')
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(read).toHaveBeenCalled())
    await act(async () => {
      await failed.catch(() => {})
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(screen.getByRole('checkbox', { name: 'Weight' }))
    expect(await screen.findByText('The file choice could not be saved. It holds on this page only.')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Weight' })).toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ format: 'pdf', fields: ['number', 'material', 'colour', 'brand', 'weight', 'qr'] })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(puts).toEqual([])
  })

  it('keeps the stored fields under a format pick', async () => {
    serve()
    const stored = ['number', 'hex', 'qr']
    const puts = serveOutput({ format: 'pdf', dpi: 300, fields: stored })
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'Brand' })).not.toBeChecked())
    await user.click(screen.getByRole('radio', { name: 'PNG' }))
    await waitFor(() => expect(puts).toEqual([{ filament_label_output: { format: 'png', dpi: 300, fields: stored } }]))
  })

  it('keeps one field', async () => {
    serve()
    const puts = serveOutput({ format: 'pdf', dpi: 300, fields: ['number', 'qr'] })
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'QR code' })).toBeChecked())
    expect(screen.getByRole('checkbox', { name: 'Number' })).toBeEnabled()
    await user.click(screen.getByRole('checkbox', { name: 'QR code' }))
    await waitFor(() => expect(puts).toEqual([{ filament_label_output: { format: 'pdf', dpi: 300, fields: ['number'] } }]))
    expect(screen.getByRole('checkbox', { name: 'Number' })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'Number' })).toBeChecked()
    expect(screen.getByTestId('label').querySelector('svg')).toBeNull()
  })

  it('place labels ignore the fields', async () => {
    serveOutput({ format: 'pdf', dpi: 300, fields: ['note'] })
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    await waitFor(() => expect(screen.getAllByTestId('label')).toHaveLength(1))
    expect(screen.getByTestId('label').querySelector('svg')).not.toBeNull()
    expect(screen.getByTestId('label')).toHaveTextContent(/^L-/)
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.getByText('A code label prints its code, its name and its QR code.')).toBeInTheDocument()
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
    // Print reads the template list first (R-7216-21).
    await waitFor(() => expect(print).toHaveBeenCalledTimes(1))
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
    expect(screen.getByRole('button', { name: 'Print' })).toBeDisabled()
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
    expect(screen.getByRole('button', { name: 'Print' })).toBeDisabled()
  })

  it('goes back to the Locations tab from a place label', () => {
    renderPage(<LabelsPage />, { route: `/filament/labels?locations=${L.AMS_A1}` })
    expect(screen.getByRole('link', { name: /Locations/ })).toHaveAttribute('href', '/filament/locations')
  })
})

describe('saved sheets (#7216)', () => {
  const SHEET_ID = '22222222-2222-4222-8222-222222222222'
  const sheet = (id: string, name: string, layout: object = {}) => ({
    id,
    name,
    layout: { page: 'letter', width_mm: 60, height_mm: 30, cols: 2, rows: 5, top_mm: 10, left_mm: 5, col_gap_mm: 2, row_gap_mm: 1, ...layout },
    created_at: '2026-11-25T10:00:00Z',
    updated_at: '2026-11-25T10:00:00Z',
  })

  /** The template list with these saved sheets, read again at each GET. */
  function serveSaved(list: () => object[]) {
    server.use(http.get('*/api/v1/filament/label-templates', () => HttpResponse.json({ ...LABEL_TEMPLATES, saved: list() })))
  }

  it('lists saved sheets in the template picker', async () => {
    serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    serveSaved(() => [sheet(SHEET_ID, 'Shelf strip')])
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'New sheet…' })).toBeInTheDocument())
    await new Promise((resolve) => setTimeout(resolve, 20))
    await user.click(screen.getByRole('combobox', { name: 'Label' }))
    const group = await screen.findByRole('group', { name: 'Saved sheets' })
    expect(within(group).getByRole('option', { name: 'Shelf strip' })).toBeInTheDocument()
  })

  it('prints a saved sheet on its grid', async () => {
    server.use(http.get('*/api/v1/filament/spools/:id', ({ params }) => HttpResponse.json(detail(String(params.id), 1))))
    serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    serveSaved(() => [sheet(SHEET_ID, 'Shelf strip')])
    const ids = Array.from({ length: 22 }, (_, i) => `s${i}`).join(',')
    renderPage(<LabelsPage />, { route: `/filament/labels?ids=${ids}` })
    await waitFor(() => expect(screen.getAllByTestId('print-label')).toHaveLength(22))
    // 2 x 5 a sheet: three pages.
    await waitFor(() => expect(document.querySelectorAll('#label-print-root > [data-sheet-page]')).toHaveLength(3))
    expect(screen.getAllByTestId('label')[0]!.getAttribute('style')).toMatch(/width: 60mm; height: 30mm/)
    expect(document.querySelector('style')?.textContent).toContain('size: 215.9mm 279.4mm')
    expect(document.querySelector('style')?.textContent).toContain('grid-template-columns: repeat(2, 60mm)')
    expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent('Shelf strip')
  })

  it('saves a new sheet and chooses it', async () => {
    serve()
    const puts = serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    const sheets: object[] = []
    serveSaved(() => sheets)
    server.use(
      http.post('*/api/v1/filament/label-sheets', async ({ request }) => {
        const body = (await request.json()) as { name: string; layout: object }
        const made = sheet(SHEET_ID, body.name, body.layout)
        sheets.push(made)
        return HttpResponse.json(made, { status: 201 })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await user.click(await screen.findByRole('button', { name: 'New sheet…' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Name'), 'Drawer fronts')
    await user.click(within(dialog).getByRole('button', { name: 'Save sheet' }))
    await waitFor(() => expect(puts).toEqual([{ filament_label_template: { template: 'saved', sheet_id: SHEET_ID } }]))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent('Drawer fronts'))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps the sheet id of a stored saved choice when it saves the choice again', async () => {
    serve()
    const puts = serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    serveSaved(() => [sheet(SHEET_ID, 'Shelf strip')])
    server.use(http.put('*/api/v1/filament/label-sheets/:id', async ({ request }) => HttpResponse.json(sheet(SHEET_ID, ((await request.json()) as { name: string }).name))))
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await user.click(await screen.findByRole('button', { name: 'Edit sheet…' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Save sheet' }))
    await waitFor(() => expect(puts).toEqual([{ filament_label_template: { template: 'saved', sheet_id: SHEET_ID } }]))
  })

  it('deletes a saved sheet after a confirm', async () => {
    serve()
    const puts = serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    let sheets = [sheet(SHEET_ID, 'Shelf strip')]
    serveSaved(() => sheets)
    const deleted: string[] = []
    server.use(
      http.delete('*/api/v1/filament/label-sheets/:id', ({ params }) => {
        deleted.push(String(params.id))
        sheets = []
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(deleted).toEqual([])
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete sheet' }))
    await waitFor(() => expect(deleted).toEqual([SHEET_ID]))
    // The page goes back to the default label and stores that choice.
    await waitFor(() => expect(puts).toEqual([{ filament_label_template: { template: 'roll_50x25' } }]))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent('Roll label 50 x 25 mm'))
    await user.click(screen.getByRole('combobox', { name: 'Label' }))
    await screen.findAllByRole('option')
    expect(screen.queryByRole('option', { name: 'Shelf strip' })).toBeNull()
  })

  it('keeps a sheet saved before the template list answers', async () => {
    serve()
    const puts = serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    const sheets: object[] = []
    const { settled } = watchTemplateReads()
    // The first GET (the mount read) waits until the test lets it answer, with the list from before the save.
    let releaseFirst!: () => void
    const firstHeld = new Promise<void>((resolve) => (releaseFirst = resolve))
    let gets = 0
    server.use(
      http.get('*/api/v1/filament/label-templates', async () => {
        gets += 1
        if (gets === 1) {
          const before = [...sheets]
          await firstHeld
          return HttpResponse.json({ ...LABEL_TEMPLATES, saved: before })
        }
        return HttpResponse.json({ ...LABEL_TEMPLATES, saved: sheets })
      }),
      http.post('*/api/v1/filament/label-sheets', async ({ request }) => {
        const body = (await request.json()) as { name: string; layout: object }
        const made = sheet(SHEET_ID, body.name, body.layout)
        sheets.push(made)
        return HttpResponse.json(made, { status: 201 })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await user.click(await screen.findByRole('button', { name: 'New sheet…' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Name'), 'Drawer fronts')
    await user.click(within(dialog).getByRole('button', { name: 'Save sheet' }))
    await waitFor(() => expect(puts).toEqual([{ filament_label_template: { template: 'saved', sheet_id: SHEET_ID } }]))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent('Drawer fronts'))
    // The mount read answers last, without the new sheet: the page keeps the newer list.
    act(() => releaseFirst())
    await settled(gets)
    expect(screen.queryByText('The saved sheet was deleted. Choose another template.')).toBeNull()
    expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent('Drawer fronts')
  })

  it('does not bring back a sheet deleted just after its save, when the list reads answer out of order', async () => {
    serve()
    serveTemplate({ template: 'roll_50x25', width_mm: null, height_mm: null })
    let sheets: object[] = []
    const { settled } = watchTemplateReads()
    let gets = 0
    // The GET after the save waits until the test lets it answer, with the list it read at its start.
    let releaseSaveRead!: () => void
    const saveReadHeld = new Promise<void>((resolve) => (releaseSaveRead = resolve))
    let holdNext = false
    server.use(
      http.get('*/api/v1/filament/label-templates', async () => {
        gets += 1
        const now = [...sheets]
        if (holdNext) {
          holdNext = false
          await saveReadHeld
        }
        return HttpResponse.json({ ...LABEL_TEMPLATES, saved: now })
      }),
      http.post('*/api/v1/filament/label-sheets', async ({ request }) => {
        const body = (await request.json()) as { name: string; layout: object }
        const made = sheet(SHEET_ID, body.name, body.layout)
        sheets.push(made)
        holdNext = true
        return HttpResponse.json(made, { status: 201 })
      }),
      http.delete('*/api/v1/filament/label-sheets/:id', () => {
        sheets = []
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent('Roll label 50 x 25 mm'))
    await new Promise((resolve) => setTimeout(resolve, 20))
    await user.click(await screen.findByRole('button', { name: 'New sheet…' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText('Name'), 'Drawer fronts')
    await user.click(within(dialog).getByRole('button', { name: 'Save sheet' }))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent('Drawer fronts'))
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete sheet' }))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent('Roll label 50 x 25 mm'))
    await new Promise((resolve) => setTimeout(resolve, 20))
    // The read after the save answers last, with the deleted sheet in it.
    act(() => releaseSaveRead())
    await settled(gets)
    await user.click(screen.getByRole('combobox', { name: 'Label' }))
    await screen.findAllByRole('option')
    expect(screen.queryByRole('option', { name: 'Drawer fronts' })).toBeNull()
  })

  it('keeps a pick made while a delete of the chosen sheet is in flight', async () => {
    serve()
    const puts = serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    let sheets = [sheet(SHEET_ID, 'Shelf strip')]
    const { calls } = watchTemplateReads()
    let gets = 0
    server.use(
      http.get('*/api/v1/filament/label-templates', () => {
        gets += 1
        return HttpResponse.json({ ...LABEL_TEMPLATES, saved: sheets })
      }),
    )
    let releaseDelete!: () => void
    const deleteHeld = new Promise<void>((resolve) => (releaseDelete = resolve))
    let deleteArrived = false
    server.use(
      http.delete('*/api/v1/filament/label-sheets/:id', async () => {
        deleteArrived = true
        await deleteHeld
        sheets = []
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent('Shelf strip'))
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete sheet' }))
    await waitFor(() => expect(deleteArrived).toBe(true))
    await choose(user, screen.getByRole('combobox', { name: 'Label' }), /Avery L7160/)
    await waitFor(() => expect(puts).toEqual([{ filament_label_template: { template: 'avery_l7160' } }]))
    const before = gets
    const started = calls.length
    act(() => releaseDelete())
    // The page reads the list once the delete has answered, after it checked the choice: a save of the default
    // label would have started before that read.
    await waitFor(() => expect(gets).toBe(before + 1))
    expect(calls.slice(started).filter((call) => call.startsWith('PUT'))).toEqual([])
    expect(screen.getByRole('combobox', { name: 'Label' })).toHaveTextContent(/Avery L7160/)
    expect(puts).toEqual([{ filament_label_template: { template: 'avery_l7160' } }])
  })

  /** The grid the print copy has: its `grid-template-columns` rule. */
  const printGrid = () => /grid-template-columns: repeat\(\d+, [\d.]+mm\)/.exec(document.querySelector('style')?.textContent ?? '')?.[0]

  it('reads the saved sheets again when the tab is shown again (R-7216-21)', async () => {
    serve()
    serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    let sheets = [sheet(SHEET_ID, 'Shelf strip')]
    serveSaved(() => sheets)
    const { calls } = watchTemplateReads()
    const listReads = () => calls.filter((call) => call.startsWith('GET') && call.includes('/filament/label-templates')).length
    renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(printGrid()).toBe('grid-template-columns: repeat(2, 60mm)'))
    // The agent edits the sheet while the page is open.
    sheets = [sheet(SHEET_ID, 'Shelf strip', { cols: 3 })]
    const before = listReads()
    // A tab that is hidden reads nothing (the page starts a read at once, so none started is none).
    const hidden = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    expect(listReads()).toBe(before)
    hidden.mockReturnValue('visible')
    act(() => void document.dispatchEvent(new Event('visibilitychange')))
    await waitFor(() => expect(printGrid()).toBe('grid-template-columns: repeat(3, 60mm)'))
  })

  it('reads the saved sheets before Print, and prints on the newest grid (R-7216-21)', async () => {
    serve()
    serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    let sheets = [sheet(SHEET_ID, 'Shelf strip')]
    serveSaved(() => sheets)
    const grids: (string | undefined)[] = []
    vi.spyOn(window, 'print').mockImplementation(() => void grids.push(printGrid()))
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(printGrid()).toBe('grid-template-columns: repeat(2, 60mm)'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Print' })).toBeEnabled())
    sheets = [sheet(SHEET_ID, 'Shelf strip', { cols: 3 })]
    await user.click(screen.getByRole('button', { name: 'Print' }))
    await waitFor(() => expect(grids).toEqual(['grid-template-columns: repeat(3, 60mm)']))
  })

  it('prints with the grid it has when the read before Print fails (R-7216-21)', async () => {
    serve()
    serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    let fail = false
    server.use(
      http.get('*/api/v1/filament/label-templates', () =>
        fail ? HttpResponse.json({ detail: 'no' }, { status: 500 }) : HttpResponse.json({ ...LABEL_TEMPLATES, saved: [sheet(SHEET_ID, 'Shelf strip')] }),
      ),
    )
    const grids: (string | undefined)[] = []
    vi.spyOn(window, 'print').mockImplementation(() => void grids.push(printGrid()))
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(printGrid()).toBe('grid-template-columns: repeat(2, 60mm)'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Print' })).toBeEnabled())
    fail = true
    await user.click(screen.getByRole('button', { name: 'Print' }))
    await waitFor(() => expect(grids).toEqual(['grid-template-columns: repeat(2, 60mm)']))
  })

  it('prints with the grid it has when the read before Print never answers (R-7216-21)', async () => {
    serve()
    serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    let hold = false
    server.use(
      http.get('*/api/v1/filament/label-templates', async () => {
        if (hold) await new Promise(() => {})
        return HttpResponse.json({ ...LABEL_TEMPLATES, saved: [sheet(SHEET_ID, 'Shelf strip')] })
      }),
    )
    const grids: (string | undefined)[] = []
    vi.spyOn(window, 'print').mockImplementation(() => void grids.push(printGrid()))
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(printGrid()).toBe('grid-template-columns: repeat(2, 60mm)'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Print' })).toBeEnabled())
    printTiming.readWaitMs = 50
    hold = true
    try {
      await user.click(screen.getByRole('button', { name: 'Print' }))
      await waitFor(() => expect(grids).toEqual(['grid-template-columns: repeat(2, 60mm)']))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Print' })).toBeEnabled())
    } finally {
      printTiming.readWaitMs = 5_000
    }
  })

  it('prints on the grid of a read that started while Print waited (R-7216-21)', async () => {
    serve()
    serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    let cols = 2
    let releaseFirst: () => void = () => {}
    let reads = 0
    server.use(
      http.get('*/api/v1/filament/label-templates', async () => {
        reads += 1
        const body = { ...LABEL_TEMPLATES, saved: [sheet(SHEET_ID, 'Shelf strip', { cols })] }
        // The read Print starts is held; a newer read answers at once with the newer grid.
        if (reads === 2) await new Promise<void>((resolve) => (releaseFirst = resolve))
        return HttpResponse.json(body)
      }),
    )
    const grids: (string | undefined)[] = []
    vi.spyOn(window, 'print').mockImplementation(() => void grids.push(printGrid()))
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    await waitFor(() => expect(printGrid()).toBe('grid-template-columns: repeat(2, 60mm)'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Print' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Print' }))
    await waitFor(() => expect(reads).toBe(2))
    cols = 3
    document.dispatchEvent(new Event('visibilitychange'))
    await waitFor(() => expect(grids).toEqual(['grid-template-columns: repeat(3, 60mm)']))
    releaseFirst()
  })

  it('shows a note when the saved sheet is gone', async () => {
    serve()
    serveTemplate({ template: 'saved', sheet_id: SHEET_ID, width_mm: null, height_mm: null })
    serveSaved(() => [])
    let body: { template?: unknown } | null = null
    server.use(
      http.post('*/api/v1/filament/labels', async ({ request }) => {
        body = (await request.json()) as { template?: unknown }
        return HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail: 'The saved sheet was deleted. Choose another template.', code: 'label_sheet_gone' },
          { status: 409 },
        )
      }),
    )
    const { user } = renderPage(<LabelsPage />, { route: '/filament/labels?ids=a' })
    expect(await screen.findByText('The saved sheet was deleted. Choose another template.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 50mm; height: 25mm/))
    expect(screen.queryByRole('button', { name: 'Edit sheet…' })).toBeNull()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Download' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The saved sheet was deleted. Choose another template.')
    expect(body!.template).toEqual({ template: 'saved', sheet_id: SHEET_ID })
  })
})

describe('the label printer row (#7170)', () => {
  it('says the label server does not answer and keeps Print and Download enabled', async () => {
    server.use(
      http.get('*/api/v1/label-printing/queues', () =>
        HttpResponse.json({ title: 'Bad Gateway', status: 502, detail: 'The label print server did not answer.', reason: 'label_server_unreachable' }, { status: 502 }),
      ),
    )
    serve()
    renderPage(<LabelsPage />, { route: '/filament/labels?ids=a,b' })
    expect(await screen.findByText(/The label print server does not answer/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Print to label printer' })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled())
    expect(screen.getByRole('button', { name: 'Print' })).toBeEnabled()
  })
})
