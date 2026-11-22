import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HttpResponse, http } from 'msw'
import { api } from '../api/client'
import type { ModelSummary, Output } from '../api/types'
import { mockModels, resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { MeshSourceDialog } from './MeshSourceDialog'

// #6916 — "Open a mesh as a model" takes the mesh from every place Import print reads a
// 3MF: the disk and a session's workspace, a library file, a print (its archive's file,
// its slicer project, a library match or the printer's storage), and an output.

function made(name: string): ModelSummary {
  return { ...(mockModels()[0] as ModelSummary), slug: name.toLowerCase(), name }
}

function render() {
  const onOpened = vi.fn()
  const onClose = vi.fn()
  const view = renderPage(<MeshSourceDialog open onClose={onClose} onOpened={onOpened} />)
  const dialog = screen.getByRole('dialog', { name: 'Open a mesh as a model' })
  return { ...view, dialog, onOpened, onClose }
}

describe('MeshSourceDialog', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('offers every source, and starts on the computer', async () => {
    const { dialog } = render()
    const from = within(dialog).getByRole('radiogroup', { name: 'Where is the mesh?' })
    expect(within(from).getAllByRole('radio').map((r) => r.textContent)).toEqual(['This computer', 'Library', 'A print', 'An output'])
    expect(within(dialog).getByLabelText('Upload a file')).toBeInTheDocument()
  })

  it('opens a library file found by name, named after it', async () => {
    server.use(
      http.get('/api/v1/print/library/search', () =>
        HttpResponse.json({
          query: 'bracket',
          all: true,
          total: 3,
          hidden: 0,
          files: [
            { id: 41, filename: 'bracket.3mf', file_type: '3mf', printable: true, folder_path: [] },
            { id: 42, filename: 'bracket.gcode.3mf', file_type: 'gcode.3mf', printable: true, sliced: true, folder_path: [] },
            { id: 43, filename: 'bracket.png', file_type: 'png', printable: false, folder_path: [] },
          ],
        }),
      ),
    )
    const create = vi.spyOn(api, 'createMeshModel').mockResolvedValue(made('bracket'))
    const { dialog, user, onOpened } = render()
    await user.click(within(dialog).getByRole('radio', { name: 'Library' }))
    await user.type(within(dialog).getByLabelText('Find a file by name'), 'bracket')
    const found = await within(dialog).findByRole('group', { name: 'Library files found' })
    // A sliced file and a picture have no mesh to open.
    expect(within(found).getAllByRole('button').map((b) => b.textContent)).toEqual(['bracket.3mf'])
    await user.click(within(found).getByRole('button', { name: 'bracket.3mf' }))
    expect(within(dialog).getByLabelText('Name')).toHaveValue('bracket')
    await user.click(within(dialog).getByRole('button', { name: 'Open as a model' }))
    await waitFor(() => expect(onOpened).toHaveBeenCalledWith(expect.objectContaining({ slug: 'bracket' })))
    expect(create).toHaveBeenCalledWith({ name: 'bracket', description: '', library_file_id: 41 })
  })

  it("opens a print's slicer project, and refuses its sliced file", async () => {
    const create = vi.spyOn(api, 'createMeshModel').mockResolvedValue(made('Benchy'))
    const { dialog, user, onOpened } = render()
    await user.click(within(dialog).getByRole('radio', { name: 'A print' }))
    await user.click(await within(dialog).findByTestId('past-9104'))
    const sources = await within(dialog).findByRole('radiogroup', { name: 'Source' })
    expect(within(sources).getByRole('radio', { name: /The archive's file/ })).toBeDisabled()
    expect(within(dialog).getAllByText('A sliced file has no meshes to open as a model.').length).toBeGreaterThan(0)
    expect(within(dialog).getByRole('button', { name: 'Open as a model' })).toBeDisabled()
    await user.click(within(sources).getByRole('radio', { name: /slicer project/ }))
    expect(within(dialog).getByLabelText('Name')).toHaveValue('benchy')
    await user.clear(within(dialog).getByLabelText('Name'))
    await user.type(within(dialog).getByLabelText('Name'), 'Benchy')
    await user.click(within(dialog).getByRole('button', { name: 'Open as a model' }))
    await waitFor(() => expect(onOpened).toHaveBeenCalled())
    expect(create).toHaveBeenCalledWith({
      name: 'Benchy',
      description: '',
      archive_id: 9104,
      archive_source: { kind: 'archive_source', library_file_id: null, path: null },
    })
  })

  it('"Choose another print" clears the chosen source, so no stale print is sent', async () => {
    const create = vi.spyOn(api, 'createMeshModel')
    const { dialog, user } = render()
    await user.click(within(dialog).getByRole('radio', { name: 'A print' }))
    await user.click(await within(dialog).findByTestId('past-9104'))
    await user.click(await within(dialog).findByRole('radio', { name: /slicer project/ }))
    expect(within(dialog).getByRole('button', { name: 'Open as a model' })).toBeEnabled()
    await user.click(within(dialog).getByRole('button', { name: 'Choose another print' }))
    expect(await within(dialog).findByTestId('past-9105')).toBeInTheDocument()
    expect(within(dialog).queryByLabelText('Name')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Open as a model' })).toBeDisabled()
    await user.click(within(dialog).getByTestId('past-9105'))
    await within(dialog).findByRole('radiogroup', { name: 'Source' })
    expect(within(dialog).getByRole('button', { name: 'Open as a model' })).toBeDisabled()
    expect(create).not.toHaveBeenCalled()
  })

  it('clears the chosen file when the source changes', async () => {
    const create = vi.spyOn(api, 'createMeshModel')
    const { dialog, user } = render()
    await user.click(within(dialog).getByRole('radio', { name: 'A print' }))
    await user.click(await within(dialog).findByTestId('past-9104'))
    await user.click(await within(dialog).findByRole('radio', { name: /slicer project/ }))
    expect(within(dialog).getByLabelText('Name')).toHaveValue('benchy')
    await user.click(within(dialog).getByRole('radio', { name: 'An output' }))
    expect(within(dialog).queryByLabelText('Name')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Open as a model' })).toBeDisabled()
    expect(create).not.toHaveBeenCalled()
  })

  it("reads the printer's storage only when told to, as Import print does", async () => {
    const read = vi.spyOn(api, 'getPrintSources')
    const { dialog, user } = render()
    await user.click(within(dialog).getByRole('radio', { name: 'A print' }))
    await user.click(await within(dialog).findByTestId('past-9105'))
    await within(dialog).findByRole('radiogroup', { name: 'Source' })
    expect(read).toHaveBeenLastCalledWith(9105, { printer: false })
    await user.click(within(dialog).getByRole('button', { name: 'Also look on the printer' }))
    // The printer's copy is the sliced job too: listed, and refused.
    expect(await within(dialog).findByRole('radio', { name: /On the printer's storage/ })).toBeDisabled()
    expect(read).toHaveBeenLastCalledWith(9105, { printer: true })
  })

  it('opens an output found by name', async () => {
    const output = { id: 'a'.repeat(32), slug: 'demo', name: 'Blue hook', created_at: new Date().toISOString(), has_thumbnail: false } as Output
    server.use(http.get('/api/v1/outputs', () => HttpResponse.json([output], { headers: { 'X-Total-Count': '1' } })))
    const create = vi.spyOn(api, 'createMeshModel').mockResolvedValue(made('Blue hook'))
    const { dialog, user } = render()
    await user.click(within(dialog).getByRole('radio', { name: 'An output' }))
    await user.click(await within(dialog).findByRole('button', { name: /Blue hook/ }))
    expect(within(dialog).getByLabelText('Name')).toHaveValue('Blue hook')
    await user.click(within(dialog).getByRole('button', { name: 'Open as a model' }))
    await waitFor(() => expect(create).toHaveBeenCalledWith({ name: 'Blue hook', description: '', output_id: output.id }))
  })

  it('shows what the route refused, and stays open', async () => {
    vi.spyOn(api, 'createMeshModel').mockRejectedValue(new Error("a model named 'benchy' already exists"))
    const { dialog, user, onClose, onOpened } = render()
    await user.click(within(dialog).getByRole('radio', { name: 'A print' }))
    await user.click(await within(dialog).findByTestId('past-9104'))
    await user.click(await within(dialog).findByRole('radio', { name: /slicer project/ }))
    await user.click(within(dialog).getByRole('button', { name: 'Open as a model' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent("a model named 'benchy' already exists")
    expect(onOpened).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })
})
