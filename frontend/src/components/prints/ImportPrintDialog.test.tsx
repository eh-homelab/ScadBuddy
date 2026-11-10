import { screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HttpResponse, http } from 'msw'
import { api, printRunPoll } from '../../api/client'
import type { ArchiveOutcome, ImportedPrint, ModelSummary } from '../../api/types'
import { addMockModel, mockModels, resetMockState } from '../../mocks/handlers'
import { archiveImports } from '../../mocks/features/orders/pastPrints'
import { server } from '../../mocks/server'
import { selectOption } from '../../test/select'
import { renderPage } from '../../test/utils'
import { ImportPrintDialog } from './ImportPrintDialog'

function benchyModel(): void {
  const base = mockModels()[0] as ModelSummary
  addMockModel({ ...base, slug: 'benchy', name: 'Benchy' })
}

async function openArchive(id: number) {
  const onImported = vi.fn()
  const view = renderPage(<ImportPrintDialog open onOpenChange={() => {}} onImported={onImported} />)
  const dialog = screen.getByRole('dialog', { name: 'Import print' })
  await view.user.click(await within(dialog).findByTestId(`past-${id}`))
  return { ...view, dialog, onImported }
}

// #4698 — "Import print": an archive ScadBuddy did not print, linked to a model.
describe('ImportPrintDialog', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('lists only the archives no print link names', async () => {
    renderPage(<ImportPrintDialog open onOpenChange={() => {}} />)
    const list = await screen.findByRole('list', { name: 'Past prints' })
    await waitFor(() => expect(within(list).getAllByRole('button')).toHaveLength(2))
    expect(within(list).getByTestId('past-9104')).toHaveTextContent('Benchy')
    expect(within(list).getByTestId('past-9105')).toHaveTextContent('Vase')
  })

  it('chooses the only usable source, and says why the others cannot be used', async () => {
    const { dialog } = await openArchive(9105)
    const sources = await within(dialog).findByRole('radiogroup', { name: 'Source' })
    expect(within(sources).getByRole('radio', { name: /The archive's file/ })).toBeChecked()
    expect(within(sources).getByRole('radio', { name: /slicer project/ })).toBeDisabled()
    expect(within(dialog).getByText('No slicer project is attached to this archive')).toBeInTheDocument()
  })

  it('asks the printer only when told to', async () => {
    const read = vi.spyOn(api, 'getPrintSources')
    const { dialog, user } = await openArchive(9105)
    await within(dialog).findByRole('radiogroup', { name: 'Source' })
    expect(read).toHaveBeenCalledTimes(1)
    expect(read).toHaveBeenLastCalledWith(9105, { printer: false })
    await user.click(within(dialog).getByRole('button', { name: 'Also look on the printer' }))
    expect(await within(dialog).findByRole('radio', { name: /On the printer's storage/ })).toBeEnabled()
    expect(read).toHaveBeenLastCalledWith(9105, { printer: true })
  })

  it('links the print to a suggested model', async () => {
    benchyModel()
    const imported = vi.spyOn(api, 'importPrint')
    const { dialog, user, onImported } = await openArchive(9104)
    const sources = await within(dialog).findByRole('radiogroup', { name: 'Source' })
    // Three usable sources: nothing is chosen for the person.
    expect(within(dialog).getByRole('button', { name: 'Import' })).toBeDisabled()
    await user.click(within(sources).getByRole('radio', { name: /Library file benchy\.3mf/ }))
    expect(within(dialog).getByRole('button', { name: 'Benchy' })).toHaveAttribute('aria-pressed', 'true')
    await user.click(within(dialog).getByRole('button', { name: 'Import' }))
    expect(await within(dialog).findByTestId('import-done')).toHaveTextContent('The print is now a print of benchy.')
    expect(imported).toHaveBeenCalledWith(9104, { source: { kind: 'library', library_file_id: 501, path: null }, slug: 'benchy' })
    expect(onImported).toHaveBeenCalledWith(expect.objectContaining({ archive_id: 9104, slug: 'benchy', created_model: false }))
    expect(archiveImports.get(9104)).toBe('benchy')
    // It is a print of the model now: the model's Prints tab lists it.
    const page = await api.listPrints({ slug: 'benchy' })
    expect(page.items.map((p) => p.archive_id)).toEqual([9104])
  })

  it('links the print to a model chosen from the list', async () => {
    const { dialog, user } = await openArchive(9105)
    await within(dialog).findByRole('radiogroup', { name: 'Source' })
    await selectOption(user, within(dialog).getByRole('combobox', { name: 'Model to link' }), 'Name Keychain')
    await user.click(within(dialog).getByRole('button', { name: 'Import' }))
    expect(await within(dialog).findByTestId('import-done')).toHaveTextContent('name-keychain')
  })

  it('makes a new model from the slicer project, never from a sliced file', async () => {
    const { dialog, user } = await openArchive(9104)
    const sources = await within(dialog).findByRole('radiogroup', { name: 'Source' })
    await user.click(within(sources).getByRole('radio', { name: /The archive's file/ }))
    await user.click(within(dialog).getByRole('radio', { name: 'A new model from the 3MF' }))
    expect(within(dialog).getByText(/A sliced file has no meshes/)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Import' })).toBeDisabled()
    await user.click(within(sources).getByRole('radio', { name: /slicer project/ }))
    await user.clear(within(dialog).getByLabelText('Name'))
    await user.type(within(dialog).getByLabelText('Name'), 'Benchy hull')
    await user.click(within(dialog).getByRole('button', { name: 'Import' }))
    expect(await within(dialog).findByTestId('import-done')).toHaveTextContent('benchy-hull, a new model made from its 3MF')
    expect(mockModels().some((m) => m.slug === 'benchy-hull')).toBe(true)
  })

  it('shows the refusal the route answers', async () => {
    benchyModel()
    const { dialog, user } = await openArchive(9104)
    const sources = await within(dialog).findByRole('radiogroup', { name: 'Source' })
    await user.click(within(sources).getByRole('radio', { name: /The archive's file/ }))
    // Another tab imported it onto another model meanwhile.
    archiveImports.set(9104, 'name-keychain')
    await user.click(within(dialog).getByRole('button', { name: 'Import' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('a print of another subject already')
  })

  it('keeps the source chosen for the person once the printer is listed too', async () => {
    const { dialog, user } = await openArchive(9105)
    const sources = await within(dialog).findByRole('radiogroup', { name: 'Source' })
    expect(within(sources).getByRole('radio', { name: /The archive's file/ })).toBeChecked()
    await user.click(within(dialog).getByRole('button', { name: 'Also look on the printer' }))
    expect(await within(dialog).findByRole('radio', { name: /On the printer's storage/ })).toBeEnabled()
    expect(within(sources).getByRole('radio', { name: /The archive's file/ })).toBeChecked()
  })

  it('asks the printer again after its read failed', async () => {
    const real = api.getPrintSources.bind(api)
    const read = vi.spyOn(api, 'getPrintSources')
    read.mockImplementation((id, query) =>
      query?.printer && read.mock.calls.filter(([, q]) => q?.printer).length === 1
        ? Promise.reject(new Error('the printer did not answer'))
        : real(id, query),
    )
    const { dialog, user } = await openArchive(9105)
    await within(dialog).findByRole('radiogroup', { name: 'Source' })
    await user.click(within(dialog).getByRole('button', { name: 'Also look on the printer' }))
    expect(await within(dialog).findByText(/Could not ask the printer/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Ask the printer again' }))
    expect(await within(dialog).findByRole('radio', { name: /On the printer's storage/ })).toBeEnabled()
  })

  it('opens on the picker again when an import ends after the dialog closed', async () => {
    let finish: (imported: ImportedPrint) => void = () => {}
    vi.spyOn(api, 'importPrint').mockImplementation(
      () =>
        new Promise<ImportedPrint>((resolve) => {
          finish = resolve
        }),
    )
    const onImported = vi.fn()
    function Harness() {
      const [open, setOpen] = useState(true)
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Reopen
          </button>
          <ImportPrintDialog open={open} onOpenChange={setOpen} onImported={onImported} />
        </>
      )
    }
    const { user } = renderPage(<Harness />)
    const dialog = screen.getByRole('dialog', { name: 'Import print' })
    await user.click(await within(dialog).findByTestId('past-9105'))
    await within(dialog).findByRole('radiogroup', { name: 'Source' })
    await selectOption(user, within(dialog).getByRole('combobox', { name: 'Model to link' }), 'Name Keychain')
    await user.click(within(dialog).getByRole('button', { name: 'Import' }))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    finish({ archive_id: 9105, slug: 'name-keychain', print_url: '/api/v1/prints/9105', created_model: false } as ImportedPrint)
    await waitFor(() => expect(onImported).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: 'Reopen' }))

    const again = await screen.findByRole('dialog', { name: 'Import print' })
    expect(await within(again).findByRole('list', { name: 'Past prints' })).toBeInTheDocument()
    expect(within(again).queryByTestId('import-done')).not.toBeInTheDocument()
  })

  it('reads more archives while every listed one is a ScadBuddy print (#5992)', async () => {
    const archive = (id: number, linked: boolean): ArchiveOutcome =>
      ({ id, print_name: `Print ${id}`, status: 'completed', created_at: '2026-11-01T00:00:00Z', scadbuddy_print: linked }) as ArchiveOutcome
    const asked: number[] = []
    server.use(
      http.get('/api/v1/farm/archives', ({ request }) => {
        const limit = Number(new URL(request.url).searchParams.get('limit'))
        asked.push(limit)
        const linked = Array.from({ length: 50 }, (_, i) => archive(1000 + i, true))
        return HttpResponse.json(limit <= 50 ? linked : [...linked, archive(2000, false)])
      }),
    )
    renderPage(<ImportPrintDialog open onOpenChange={() => {}} />)
    const list = await screen.findByRole('list', { name: 'Past prints' })
    expect(await within(list).findByTestId('past-2000')).toHaveTextContent('Print 2000')
    expect(asked).toEqual([50, 100])
  })

  it("stops reading at the route's most, 200 archives, when all are ScadBuddy prints (#5994)", async () => {
    const asked: number[] = []
    server.use(
      http.get('/api/v1/farm/archives', ({ request }) => {
        const limit = Number(new URL(request.url).searchParams.get('limit'))
        asked.push(limit)
        return HttpResponse.json(
          Array.from({ length: limit }, (_, i) => ({ id: 1000 + i, print_name: `Print ${i}`, status: 'completed', scadbuddy_print: true }) as ArchiveOutcome),
        )
      }),
    )
    renderPage(<ImportPrintDialog open onOpenChange={() => {}} />)
    await waitFor(() => expect(asked).toEqual([50, 100, 150, 200]))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(asked).toEqual([50, 100, 150, 200])
  })

  it('follows a slow new model, then shows the import it made (#5992)', async () => {
    printRunPoll.intervalMs = 1
    const keys: (string | null)[] = []
    const made = { id: 'op-3', kind: 'mesh_model_create', subject: 'benchy-hull', status: 'running', created_at: '2026-11-10T00:00:00Z' }
    server.use(
      http.post('/api/v1/print-imports/9104', ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return keys.length < 2
          ? HttpResponse.json(made, { status: 202 })
          : HttpResponse.json(
              { archive_id: 9104, slug: 'benchy-hull', print_url: '/api/v1/prints/9104', created_model: true, made_now: true, imported: { archive_id: 9104, slug: 'benchy-hull', source: 'archive_source', filename: 'benchy.3mf', created_model: true } },
              { status: 201 },
            )
      }),
      http.get('/api/v1/operations/op-3', () => HttpResponse.json({ ...made, status: 'succeeded', result: { slug: 'benchy-hull', name: 'Benchy hull' } })),
    )
    try {
      const { dialog, user } = await openArchive(9104)
      const sources = await within(dialog).findByRole('radiogroup', { name: 'Source' })
      await user.click(within(sources).getByRole('radio', { name: /slicer project/ }))
      await user.click(within(dialog).getByRole('radio', { name: 'A new model from the 3MF' }))
      await user.clear(within(dialog).getByLabelText('Name'))
      await user.type(within(dialog).getByLabelText('Name'), 'Benchy hull')
      await user.click(within(dialog).getByRole('button', { name: 'Import' }))
      expect(await within(dialog).findByTestId('import-done')).toHaveTextContent('benchy-hull, a new model made from its 3MF')
      expect(keys).toHaveLength(2)
      expect(keys[1]).toBe(keys[0])
    } finally {
      printRunPoll.intervalMs = 1000
    }
  })
})
