import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { ModelSummary } from '../../api/types'
import { addMockModel, mockModels, resetMockState } from '../../mocks/handlers'
import { NO_MODEL_MATCHED, NO_SOURCE } from '../../mocks/features/printWatch'
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
})
