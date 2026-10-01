import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes, useLocation, useNavigate, useParams } from 'react-router'
import { afterEach, describe, expect, it } from 'vitest'
import type { Output, PrintPage, PrintProgress } from '../api/types'
import { outputs, prints, queuedSliceProgress } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { PRINTS_VIEW_KEY, resetStoredPrintsView } from '../lib/printsQuery'
import { renderPage } from '../test/utils'
import { App } from '../App'
import { PrintsPage, TemplatePrintsPage } from './PrintsPage'

/** Where the router is, so a test can read the URL the filters wrote. */
function Location() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

/** Changes the URL from outside the filter bar, as Back/Forward does. */
function Elsewhere() {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => void navigate('/prints?status=failed')}>
      Navigate elsewhere
    </button>
  )
}

function PrintRoute() {
  return <div data-testid="print-route">{useParams()['archiveId']}</div>
}

function render(route = '/prints') {
  return renderPage(
    <>
      <Routes>
        <Route path="/prints" element={<PrintsPage />} />
        <Route path="/prints/:archiveId" element={<PrintRoute />} />
        <Route path="/m/:slug/prints" element={<TemplatePrintsPage />} />
      </Routes>
      <Location />
      <Elsewhere />
    </>,
    { route },
  )
}

function location(): string {
  return screen.getByTestId('location').textContent ?? ''
}

/** The rendered prints, by archive id, in order. */
async function shown(): Promise<string[]> {
  const list = await screen.findByRole('list', { name: 'Prints' })
  return within(list)
    .getAllByRole('listitem')
    .map((item) => item.getAttribute('data-print') ?? '')
    .filter(Boolean)
}

async function item(archiveId: number): Promise<HTMLElement> {
  await screen.findByRole('list', { name: 'Prints' })
  const found = document.querySelector(`[data-print="${archiveId}"]`)
  if (!(found instanceof HTMLElement)) throw new Error(`no print ${archiveId}`)
  return found
}

afterEach(() => {
  resetStoredPrintsView()
  localStorage.clear()
})

describe('PrintsPage (#310): the global print history', () => {
  it('lists every print newest first, the deleted one included', async () => {
    render()
    await waitFor(async () => expect(await shown()).toEqual(['38', '37', '36', '35']))
    expect(await item(35)).toHaveTextContent('Succeeded')
    expect(await item(36)).toHaveTextContent('Failed')
    expect(await item(37)).toHaveTextContent('Printing')
    expect(await item(38)).toHaveTextContent('Deleted in Bambuddy')
  })

  it('shows the printer, duration, filament, params and timelapse of a print', async () => {
    render()
    const done = await item(35)
    expect(done).toHaveTextContent('1h 47m')
    expect(done).toHaveTextContent('16.4 g')
    expect(done).toHaveTextContent('3DP-31B-598')
    expect(done).not.toHaveTextContent('Printer 1')
    expect(within(done).getByLabelText('Has a timelapse')).toBeInTheDocument()
    expect(within(done).getByText('name')).toBeInTheDocument()
    expect(within(done).getByText('Reagan', { selector: 'dd' })).toBeInTheDocument()
    expect(within(await item(36)).queryByLabelText('Has a timelapse')).not.toBeInTheDocument()
  })

  it('names the printer, and falls back to its id where Bambuddy gave no name', async () => {
    server.use(
      http.get('/api/v1/prints', () =>
        HttpResponse.json({
          items: [{ ...summaryOf(35), printer_id: 3, printer_name: null }],
          next_cursor: null,
        } satisfies PrintPage),
      ),
    )
    render('/prints?printer=3')
    expect(await item(35)).toHaveTextContent('Printer 3')
    expect(within(screen.getByLabelText('Printer')).getByRole('option', { name: 'Printer 3' })).toHaveValue('3')
  })

  it('names the attachments a print has', async () => {
    server.use(
      http.get('/api/v1/prints', () =>
        HttpResponse.json({
          items: [{ ...summaryOf(35), attachment_count: 3 }],
          next_cursor: null,
        } satisfies PrintPage),
      ),
    )
    render()
    expect(within(await item(35)).getByLabelText('3 attachments')).toHaveTextContent('3')
  })

  it('filters by status, and writes the filter into the URL', async () => {
    const { user } = render()
    await waitFor(async () => expect(await shown()).toHaveLength(4))
    await user.selectOptions(screen.getByLabelText('Status'), 'failed')
    await waitFor(async () => expect(await shown()).toEqual(['36']))
    expect(location()).toBe('/prints?status=failed')
  })

  it('arrives filtered from the URL, and filters by template and printer', async () => {
    const { user } = render('/prints?printer=2')
    await waitFor(async () => expect(await shown()).toEqual(['37']))
    expect(screen.getByLabelText('Printer')).toHaveValue('2')
    expect(await within(screen.getByLabelText('Printer')).findByRole('option', { name: '3DP-H2C-042' })).toHaveValue('2')

    await user.selectOptions(screen.getByLabelText('Printer'), '')
    await waitFor(async () => expect(await shown()).toHaveLength(4))
    const template = screen.getByLabelText('Template')
    await waitFor(() =>
      expect(within(template).getByRole('option', { name: 'Name Keychain' })).toBeInTheDocument(),
    )
    await user.selectOptions(template, 'name-keychain')
    expect(location()).toBe('/prints?slug=name-keychain')
  })

  it('filters by date range', async () => {
    const { user } = render()
    await waitFor(async () => expect(await shown()).toHaveLength(4))
    await user.type(screen.getByLabelText('From'), '2026-09-27')
    await waitFor(async () => expect(await shown()).toEqual(['37', '35']))
    expect(location()).toBe('/prints?from=2026-09-27')
  })

  it('searches by text after a pause, and clears every filter', async () => {
    const { user } = render('/prints?status=printing')
    await waitFor(async () => expect(await shown()).toEqual(['37']))
    await user.selectOptions(screen.getByLabelText('Status'), '')
    await user.type(screen.getByRole('searchbox', { name: 'Search prints' }), 'nova')
    await waitFor(async () => expect(await shown()).toEqual(['37']))
    expect(location()).toBe('/prints?q=nova')

    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    await waitFor(async () => expect(await shown()).toHaveLength(4))
    expect(location()).toBe('/prints')
    expect(screen.getByRole('searchbox', { name: 'Search prints' })).toHaveValue('')
  })

  it('drops a pending search when the URL changes from outside, as back/forward does', async () => {
    const { user } = render()
    await waitFor(async () => expect(await shown()).toHaveLength(4))
    await user.type(screen.getByRole('searchbox', { name: 'Search prints' }), 'nov')
    await user.click(screen.getByRole('button', { name: 'Navigate elsewhere' }))
    await waitFor(async () => expect(await shown()).toEqual(['36']))
    // Past the debounce: the abandoned search must not come back onto the new URL.
    await new Promise((resolve) => setTimeout(resolve, 400))
    expect(location()).toBe('/prints?status=failed')
    expect(screen.getByRole('searchbox', { name: 'Search prints' })).toHaveValue('')
  })

  it('keeps offering every printer seen after the list is filtered to one', async () => {
    const { user } = render()
    await waitFor(async () => expect(await shown()).toHaveLength(4))
    const printer = screen.getByLabelText('Printer')
    // The printers are gathered from the items after they render.
    await within(printer).findByRole('option', { name: '3DP-H2C-042' })
    await user.selectOptions(printer, '2')
    await waitFor(async () => expect(await shown()).toEqual(['37']))
    expect(within(printer).getByRole('option', { name: '3DP-31B-598' })).toHaveValue('1')
    expect(within(printer).getByRole('option', { name: '3DP-H2C-042' })).toHaveValue('2')
  })

  it('says when nothing matches, and offers to clear the filters', async () => {
    const { user } = render('/prints?q=nothing-like-this')
    expect(await screen.findByText('No prints match')).toBeInTheDocument()
    await user.click(screen.getAllByRole('button', { name: 'Clear filters' }).at(-1)!)
    await waitFor(async () => expect(await shown()).toHaveLength(4))
  })

  it('says when nothing has been printed yet', async () => {
    server.use(
      http.get('/api/v1/prints', () => HttpResponse.json({ items: [], next_cursor: null } satisfies PrintPage)),
    )
    render()
    expect(await screen.findByText('No prints yet')).toBeInTheDocument()
  })

  it('shows a failed load and tries again', async () => {
    let calls = 0
    server.use(
      http.get('/api/v1/prints', () => {
        calls += 1
        if (calls === 1) return HttpResponse.json({ title: 'Bad Gateway', status: 502, detail: 'Bambuddy is down' }, { status: 502 })
        return HttpResponse.json({ items: [summaryOf(35)], next_cursor: null } satisfies PrintPage)
      }),
    )
    const { user } = render()
    expect(await screen.findByRole('alert')).toHaveTextContent('Bambuddy is down')
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(async () => expect(await shown()).toEqual(['35']))
  })

  it('switches to the list and back, remembering the choice', async () => {
    const { user } = render()
    await waitFor(async () => expect(await shown()).toHaveLength(4))
    const view = screen.getByRole('group', { name: 'View' })
    await user.click(within(view).getByRole('button', { name: 'List' }))
    expect(location()).toBe('/prints?view=list')
    expect(screen.getByRole('list', { name: 'Prints' })).toHaveAttribute('data-view', 'list')
    expect(localStorage.getItem(PRINTS_VIEW_KEY)).toBe('list')
    await user.click(within(view).getByRole('button', { name: 'Cards' }))
    expect(screen.getByRole('list', { name: 'Prints' })).toHaveAttribute('data-view', 'cards')
  })

  it('opens in the view this browser last chose when the URL names none', async () => {
    localStorage.setItem(PRINTS_VIEW_KEY, 'list')
    render()
    expect(await screen.findByRole('list', { name: 'Prints' })).toHaveAttribute('data-view', 'list')
  })

  it('pages with Load more', async () => {
    server.use(
      http.get('/api/v1/prints', ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        return HttpResponse.json(
          cursor === null
            ? { items: [summaryOf(38), summaryOf(37)], next_cursor: '37' }
            : { items: [summaryOf(36), summaryOf(35)], next_cursor: null },
        )
      }),
    )
    const { user } = render()
    await waitFor(async () => expect(await shown()).toEqual(['38', '37']))
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(async () => expect(await shown()).toEqual(['38', '37', '36', '35']))
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
  })

  it('reads past an empty page that still has a cursor, on the first load', async () => {
    const asked: (string | null)[] = []
    server.use(
      http.get('/api/v1/prints', ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        asked.push(cursor)
        if (cursor === null) return HttpResponse.json({ items: [], next_cursor: '90' } satisfies PrintPage)
        return HttpResponse.json({ items: [summaryOf(35)], next_cursor: null } satisfies PrintPage)
      }),
    )
    render()
    await waitFor(async () => expect(await shown()).toEqual(['35']))
    expect(asked).toEqual([null, '90'])
    expect(screen.queryByText('No prints yet')).not.toBeInTheDocument()
  })

  it('reads past an empty page that still has a cursor, on Load more', async () => {
    server.use(
      http.get('/api/v1/prints', ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        const pages: Record<string, PrintPage> = {
          first: { items: [summaryOf(38)], next_cursor: '38' },
          '38': { items: [], next_cursor: '20' },
          '20': { items: [summaryOf(35)], next_cursor: null },
        }
        return HttpResponse.json(pages[cursor ?? 'first'])
      }),
    )
    const { user } = render()
    await waitFor(async () => expect(await shown()).toEqual(['38']))
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(async () => expect(await shown()).toEqual(['38', '35']))
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
  })

  it('offers Load more, not "No prints", while empty pages still have a cursor', async () => {
    server.use(
      http.get('/api/v1/prints', () => HttpResponse.json({ items: [], next_cursor: '90' } satisfies PrintPage)),
    )
    render()
    expect(await screen.findByRole('button', { name: 'Load more' })).toBeInTheDocument()
    expect(screen.queryByText('No prints yet')).not.toBeInTheDocument()
  })

  it('opens the print when the row is clicked', async () => {
    const { user } = render()
    const done = await item(35)
    await user.click(within(done).getByRole('link'))
    expect(await screen.findByTestId('print-route')).toHaveTextContent('35')
  })

  it('opens the lightbox on the image, with the photos and the timelapse', async () => {
    const { user } = render()
    const done = await item(35)
    await user.click(within(done).getByRole('button', { name: /^Open media of/ }))
    const dialog = await screen.findByRole('dialog', {}, { timeout: 3000 })
    await waitFor(() =>
      expect(document.querySelector('.yarl__slide_current img')).toHaveAttribute(
        'src',
        '/api/v1/prints/35/photos/finish_20260927_015703_93372185.jpg',
      ),
    )
    expect(location()).toBe('/prints')
    // The timelapse is a slide of its own.
    await waitFor(() => expect(dialog.querySelectorAll('.yarl__slide').length).toBeGreaterThan(1))
  })

  it('offers no lightbox for a print whose archive was deleted', async () => {
    render()
    const gone = await item(38)
    expect(within(gone).queryByRole('button', { name: /^Open media of/ })).not.toBeInTheDocument()
    expect(within(gone).getByTestId('print-cover')).toHaveTextContent('Archive deleted in Bambuddy')
  })

  it('says "No image" for a print Bambuddy still has but has no image of', async () => {
    server.use(
      http.get('/api/v1/prints', () =>
        HttpResponse.json({ items: [{ ...summaryOf(36), cover: null }], next_cursor: null } satisfies PrintPage),
      ),
    )
    render()
    const bare = await item(36)
    expect(within(bare).getByTestId('print-cover')).toHaveTextContent('No image')
    expect(within(bare).queryByRole('button', { name: /^Open media of/ })).not.toBeInTheDocument()
  })

  it('shows where a print in progress is, from the progress read', async () => {
    server.use(
      http.get('/api/v1/print/outputs/:id/progress', () =>
        HttpResponse.json({
          ...queuedSliceProgress,
          stage: 'running',
          copies_detail: [
            { ...queuedSliceProgress.copies_detail![0]!, stage: 'running', message: 'Layer 42 of 180', waiting_reason: null },
          ],
        } satisfies PrintProgress),
      ),
    )
    render()
    const printing = await item(37)
    expect(await within(printing).findByText('Layer 42 of 180')).toBeInTheDocument()
    // The archive's printer, named once: the progress read's is only a fallback.
    expect(printing).toHaveTextContent('3DP-H2C-042')
    expect(printing).not.toHaveTextContent('3DP-31B-598')
  })
})

describe('a sent output Bambuddy has forgotten (#898)', () => {
  const sent = (id: string, name: string, queueItemId: number): Output => ({
    ...(outputs[0] as Output),
    id: id.repeat(32),
    name,
    created_at: '2026-09-28T10:00:00Z',
    queue_item_id: queueItemId,
  })

  it('stops waiting once its progress has settled, and says why', async () => {
    const expired = sent('1', 'Acceptance', 104)
    const pending = sent('2', 'Luna', 4500)
    server.use(
      http.get('/api/v1/models/:slug/outputs', () => HttpResponse.json([expired, pending, ...outputs])),
      http.get('/api/v1/print/outputs/:id/progress', ({ params }) =>
        HttpResponse.json(
          params['id'] === expired.id
            ? ({
                ...queuedSliceProgress,
                stage: 'unknown',
                settled: true,
                queue_item_id: null,
                slice_job_id: 1,
                error_message: 'Slice job not found or expired',
              } satisfies PrintProgress)
            : ({ ...queuedSliceProgress, queue_item_id: 4500 } satisfies PrintProgress),
        ),
      ),
    )
    render('/m/name-keychain/prints')
    const list = await screen.findByRole('list', { name: 'Waiting for Bambuddy' })
    const [first, second] = within(list).getAllByRole('listitem')
    expect(first).toHaveTextContent('Acceptance')
    expect(await within(first as HTMLElement).findByText('No longer in Bambuddy')).toBeInTheDocument()
    expect(first).not.toHaveTextContent('Waiting for Bambuddy')
    expect(second).toHaveTextContent('Luna')
    expect(second).toHaveTextContent('Waiting for Bambuddy')
  })
})

describe('waiting for Bambuddy, with more pages to load (#310)', () => {
  const luna: Output = {
    ...(outputs[0] as Output),
    id: '1'.repeat(32),
    name: 'Luna',
    // A day after print 37 in any time zone: its time carries none, so it reads as local.
    created_at: '2026-09-29T12:00:00Z',
    queue_item_id: 4500,
  }

  function firstPage(items: PrintPage['items']) {
    server.use(
      http.get('/api/v1/prints', () => HttpResponse.json({ items, next_cursor: '1' } satisfies PrintPage)),
      http.get('/api/v1/models/:slug/outputs', () => HttpResponse.json([luna, ...outputs])),
    )
  }

  it('judges by the oldest print that has a time, past a deleted one without', async () => {
    firstPage([summaryOf(37), summaryOf(38)])
    render('/m/name-keychain/prints')
    const waiting = await screen.findByRole('list', { name: 'Waiting for Bambuddy' })
    expect(waiting).toHaveTextContent('Luna')
    // Reagan was sent before the oldest print loaded: its print may be on a later page.
    expect(waiting).not.toHaveTextContent('Reagan')
  })

  it('still shows them when no loaded print has a time', async () => {
    firstPage([summaryOf(38)])
    render('/m/name-keychain/prints')
    const waiting = await screen.findByRole('list', { name: 'Waiting for Bambuddy' })
    expect(waiting).toHaveTextContent('Luna')
  })
})

describe('the print route in the app (#310, #311)', () => {
  it('a row click lands on the print, not back on the catalogue', async () => {
    const { user } = renderPage(
      <>
        <App />
        <Location />
      </>,
      { route: '/prints' },
    )
    const failed = await item(36)
    await user.click(within(failed).getByRole('link'))
    expect(await screen.findByRole('heading', { name: 'Reagan' })).toBeInTheDocument()
    expect(location()).toBe('/prints/36')
    // The detail page's (#311) own way back, not the app's nav link of the same name.
    expect(within(screen.getByRole('main')).getByRole('link', { name: 'Prints' })).toHaveAttribute('href', '/prints')
    expect(screen.getAllByText('Failed').length).toBeGreaterThan(0)
  })

  it('says so for a print ScadBuddy does not know', async () => {
    renderPage(<App />, { route: '/prints/99' })
    expect(await screen.findByRole('alert')).toHaveTextContent('not a print of any ScadBuddy output')
  })

  it('says the same for an archive id that is not a number, without asking the API', async () => {
    let asked = false
    server.use(
      http.get('/api/v1/prints/:archiveId', () => {
        asked = true
        return HttpResponse.json({ title: 'Unprocessable Content', status: 422 }, { status: 422 })
      }),
    )
    renderPage(<App />, { route: '/prints/abc' })
    expect(await screen.findByRole('alert')).toHaveTextContent('not a print of any ScadBuddy output')
    expect(asked).toBe(false)
  })
})

describe('TemplatePrintsPage (#310): a template’s Prints tab', () => {
  it('lists only this template’s prints, with no template filter', async () => {
    let asked: string | null = null
    server.use(
      http.get('/api/v1/prints', ({ request }) => {
        asked = new URL(request.url).searchParams.get('slug')
        return HttpResponse.json({ items: [summaryOf(35)], next_cursor: null } satisfies PrintPage)
      }),
    )
    render('/m/name-keychain/prints?slug=other')
    await waitFor(async () => expect(await shown()).toEqual(['35']))
    expect(asked).toBe('name-keychain')
    expect(screen.queryByLabelText('Template')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Prints' })).toBeInTheDocument()
  })

  it('shows a sent output with no print yet as waiting for Bambuddy', async () => {
    const sent: Output = {
      ...(outputs[0] as Output),
      id: '1'.repeat(32),
      name: 'Luna',
      created_at: '2026-09-28T10:00:00Z',
      queue_item_id: 4500,
    }
    const unsent: Output = {
      ...(outputs[0] as Output),
      id: '2'.repeat(32),
      name: 'Draft',
      created_at: '2026-09-28T11:00:00Z',
      queue_item_id: null,
      plates: [],
    }
    server.use(
      http.get('/api/v1/models/:slug/outputs', () => HttpResponse.json([unsent, sent, ...outputs])),
    )
    render('/m/name-keychain/prints')
    const waiting = await screen.findByRole('list', { name: 'Waiting for Bambuddy' })
    expect(within(waiting).getAllByRole('listitem')).toHaveLength(1)
    expect(waiting).toHaveTextContent('Luna')
    expect(waiting).toHaveTextContent('Waiting for Bambuddy')
    // Every other sent output already has its print.
    expect(waiting).not.toHaveTextContent('Reagan')
    expect(waiting).not.toHaveTextContent('Draft')
  })
})

function summaryOf(archiveId: number): PrintPage['items'][number] {
  const print = prints.find((p) => p.archive_id === archiveId)
  if (!print) throw new Error(`no fixture print ${archiveId}`)
  const { provenance: _p, files: _f, media: _m, outcome: _o, printer_media: _pm, links: _l, ...summary } = print
  return summary
}
