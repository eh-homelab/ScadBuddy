import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { AutoImport, ModelSummary } from '../../api/types'
import { addMockModel, mockModels, resetMockState } from '../../mocks/handlers'
import { NO_MODEL_MATCHED, NO_SOURCE } from '../../mocks/features/printWatch'
import { archiveImports } from '../../mocks/features/orders/pastPrints'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { UnsortedImports } from './UnsortedImports'

// #4699 phase 2 — the print watcher's inbox on the Prints page.
describe('UnsortedImports', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('lists the inbox prints with the reason each is there', async () => {
    renderPage(<UnsortedImports />)
    const list = await screen.findByRole('list', { name: 'Unsorted imports' })
    const rows = within(list).getAllByRole('listitem')
    expect(rows.map((row) => row.dataset['testid'])).toEqual(['unsorted-9105', 'unsorted-9104'])
    expect(within(rows[0]!).getByText(NO_SOURCE)).toBeInTheDocument()
    expect(within(rows[1]!).getByText(NO_MODEL_MATCHED)).toBeInTheDocument()
    expect(within(rows[1]!).getByText('Benchy')).toBeInTheDocument()
  })

  it('shows nothing while the inbox is empty', async () => {
    server.use(http.get('/api/v1/print-imports/auto', () => HttpResponse.json([])))
    const read = vi.spyOn(api, 'listAutoImports')
    const { container } = renderPage(<UnsortedImports />)
    await waitFor(() => expect(read).toHaveBeenCalledWith({ state: 'inbox', limit: 50 }))
    await waitFor(() => expect(container.querySelector('[data-testid="unsorted-imports"]')).toBeNull())
  })

  it('dismisses a print, which leaves the inbox', async () => {
    const dismiss = vi.spyOn(api, 'dismissAutoImport')
    const { user } = renderPage(<UnsortedImports />)
    const row = await screen.findByTestId('unsorted-9105')
    await user.click(within(row).getByRole('button', { name: 'Dismiss Vase' }))
    await waitFor(() => expect(screen.queryByTestId('unsorted-9105')).toBeNull())
    expect(dismiss).toHaveBeenCalledWith(9105)
    expect(screen.getByTestId('unsorted-9104')).toBeInTheDocument()
    expect((await api.listAutoImports({ state: 'dismissed' })).map((r) => r.archive_id)).toEqual([9105])
  })

  it('says why a dismiss was refused, and keeps the print', async () => {
    server.use(
      http.post('/api/v1/print-imports/auto/:id/dismiss', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail: 'archive 9105 is a print of model:vase, not in Unsorted imports' },
          { status: 409, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderPage(<UnsortedImports />)
    const row = await screen.findByTestId('unsorted-9105')
    await user.click(within(row).getByRole('button', { name: 'Dismiss Vase' }))
    expect(await within(row).findByRole('alert')).toHaveTextContent('not in Unsorted imports')
    expect(screen.getByTestId('unsorted-9105')).toBeInTheDocument()
  })

  it('links a print through Import print, its recorded source chosen, and it leaves the inbox', async () => {
    const base = mockModels()[0] as ModelSummary
    addMockModel({ ...base, slug: 'benchy', name: 'Benchy' })
    const onImported = vi.fn()
    const { user } = renderPage(<UnsortedImports onImported={onImported} />)
    const row = await screen.findByTestId('unsorted-9104')
    await user.click(within(row).getByRole('button', { name: 'Link Benchy' }))
    const dialog = await screen.findByRole('dialog', { name: 'Import print' })
    expect(within(dialog).getByTestId('import-archive')).toHaveTextContent('Benchy')
    const sources = await within(dialog).findByRole('radiogroup', { name: 'Source' })
    // Three sources can be used, so none is preselected; the watcher's own is chosen.
    expect(within(sources).getByRole('radio', { name: /slicer project/ })).toBeChecked()
    expect(within(sources).getByRole('radio', { name: /The archive's file/ })).not.toBeChecked()
    await user.click(within(dialog).getByRole('button', { name: 'Import' }))
    expect(await within(dialog).findByTestId('import-done')).toHaveTextContent('benchy')
    expect(onImported).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.queryByTestId('unsorted-9104')).toBeNull())
  })

  it('falls back to the preselected source when the recorded one is not listed (review of #6028)', async () => {
    // A printer file with no kept library file: the dialog never reads the printer for it.
    server.use(
      http.get('/api/v1/print-imports/auto', () =>
        HttpResponse.json([
          { archive_id: 9105, print_name: 'Vase', state: 'inbox', source: 'printer', library_file_id: null, reason: 'r', suggested: [] },
        ] satisfies AutoImport[]),
      ),
    )
    const read = vi.spyOn(api, 'getPrintSources')
    const { user } = renderPage(<UnsortedImports />)
    const row = await screen.findByTestId('unsorted-9105')
    await user.click(within(row).getByRole('button', { name: 'Link Vase' }))
    const dialog = await screen.findByRole('dialog', { name: 'Import print' })
    const sources = await within(dialog).findByRole('radiogroup', { name: 'Source' })
    // The only usable source is preselected, as without a recorded one.
    expect(within(sources).getByRole('radio', { name: /The archive's file/ })).toBeChecked()
    expect(read).toHaveBeenLastCalledWith(9105, { printer: false })
  })

  it('reads the next page with Show more while a page was full', async () => {
    const page = (from: number, count: number): AutoImport[] =>
      Array.from({ length: count }, (_, i) => ({
        archive_id: from - i,
        print_name: `Job ${from - i}`,
        state: 'inbox' as const,
        reason: 'r',
        suggested: [],
      }))
    const asked: (string | null)[] = []
    server.use(
      http.get('/api/v1/print-imports/auto', ({ request }) => {
        const query = new URL(request.url).searchParams
        asked.push(query.get('before'))
        const limit = Number(query.get('limit'))
        return HttpResponse.json(query.get('before') === null ? page(2000, limit) : page(1000, 2))
      }),
    )
    const { user } = renderPage(<UnsortedImports />)
    const list = await screen.findByRole('list', { name: 'Unsorted imports' })
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(50))
    await user.click(screen.getByRole('button', { name: 'Show more' }))
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(52))
    expect(asked).toEqual([null, '1951'])
    expect(within(list).getByTestId('unsorted-999')).toBeInTheDocument()
    // A short page is the last one.
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull()
  })

  it('reads the first page again once every shown print is dismissed, so older ones still show (review of #6028)', async () => {
    // 52 inbox prints, served as the route does: newest first, dismissed ones left out.
    const inbox = new Map<number, AutoImport>(
      Array.from({ length: 52 }, (_, i) => {
        const id = 3000 - i
        return [id, { archive_id: id, print_name: `Job ${id}`, state: 'inbox' as const, reason: 'r', suggested: [] }]
      }),
    )
    server.use(
      http.get('/api/v1/print-imports/auto', ({ request }) => {
        const query = new URL(request.url).searchParams
        const before = Number(query.get('before') ?? Infinity)
        const limit = Number(query.get('limit'))
        const rows = [...inbox.values()].filter((r) => r.state === 'inbox' && r.archive_id < before)
        return HttpResponse.json(rows.slice(0, limit))
      }),
      http.post('/api/v1/print-imports/auto/:id/dismiss', ({ params }) => {
        const row = inbox.get(Number(params['id']))!
        const dismissed = { ...row, state: 'dismissed' as const }
        inbox.set(row.archive_id, dismissed)
        return HttpResponse.json(dismissed)
      }),
    )
    const { user } = renderPage(<UnsortedImports />)
    const list = await screen.findByRole('list', { name: 'Unsorted imports' })
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(50))
    for (let id = 3000; id > 2950; id--) {
      await user.click(within(screen.getByTestId(`unsorted-${id}`)).getByRole('button', { name: `Dismiss Job ${id}` }))
      await waitFor(() => expect(screen.queryByTestId(`unsorted-${id}`)).toBeNull())
    }
    expect(await screen.findByTestId('unsorted-2950')).toBeInTheDocument()
    expect(screen.getByTestId('unsorted-2949')).toBeInTheDocument()
  }, 60_000)

  it('keeps an inbox print on its own print, with no way back to the archive picker', async () => {
    const { user } = renderPage(<UnsortedImports />)
    const row = await screen.findByTestId('unsorted-9105')
    await user.click(within(row).getByRole('button', { name: 'Link Vase' }))
    const dialog = await screen.findByRole('dialog', { name: 'Import print' })
    await within(dialog).findByRole('radiogroup', { name: 'Source' })
    expect(within(dialog).queryByRole('button', { name: 'Choose another print' })).toBeNull()
  })

  it('lists no dismissed print that a link names since, as the route does', async () => {
    await api.dismissAutoImport(9105)
    archiveImports.set(9105, 'vase')
    expect(await api.listAutoImports({ state: 'dismissed' })).toEqual([])
  })
})
