import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { ModelSummary } from '../../api/types'
import { addMockModel, mockModels, resetMockState } from '../../mocks/handlers'
import { archiveImports } from '../../mocks/features/orders/pastPrints'
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
})
