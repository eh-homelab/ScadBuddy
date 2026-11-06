import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { libraryFiles, libraryFolders } from '../../mocks/features/library'
import { FAILS, SAME_HASH, startedCopies } from '../../mocks/features/libraryCopy'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { LibraryCopyDialog, POLL_MS } from './LibraryCopyDialog'

// #5355 — the Copy between libraries dialog: Bambuddy's library browsed as the source,
// the selection started as a server job, and its per-file results shown as it ends.

beforeEach(() => {
  resetMockState()
  setMockSettings({ ...mockSettings(), library_backend: 'native' })
})
afterEach(() => vi.useRealTimers())

function renderDialog(picked = libraryFiles.slice(0, 1)) {
  const onClose = vi.fn()
  const onCopied = vi.fn()
  const user = userEvent.setup()
  render(<LibraryCopyDialog open onClose={onClose} nativeFolders={libraryFolders} picked={picked} onCopied={onCopied} />)
  return { user, onClose, onCopied }
}

describe('LibraryCopyDialog', () => {
  it('says why it cannot run while the Bambuddy library serves the page', async () => {
    setMockSettings({ ...mockSettings(), library_backend: 'bambuddy' })
    renderDialog()
    expect(await screen.findByTestId('library-copy-not-native')).toHaveTextContent(/set the library backend to native/)
    expect(screen.queryByTestId('library-copy-start')).not.toBeInTheDocument()
  })

  it("copies picked Bambuddy files and a whole folder into a ScadBuddy folder, then shows each file's result", async () => {
    const { user, onCopied } = renderDialog()
    // Bambuddy's tree is the source; ScadBuddy's own (the page's) is the target.
    const source = await screen.findByRole('tree', { name: 'Source folder' })
    await user.click(await within(source).findByRole('treeitem', { name: 'Dinos' }))
    const files = await screen.findByRole('list', { name: 'Source files' })
    expect(within(files).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'rex.3mf',
      'already-here.3mf',
      'broken.gcode.3mf',
    ])
    await user.click(screen.getByTestId('library-copy-file-500'))
    await user.click(screen.getByTestId(`library-copy-file-${SAME_HASH}`))
    await user.click(screen.getByTestId(`library-copy-file-${FAILS}`))
    await user.click(screen.getByTestId('library-copy-whole-folder'))
    const target = screen.getByRole('tree', { name: 'Target folder' })
    await user.click(within(target).getByRole('treeitem', { name: 'Supplies' }))
    const start = screen.getByTestId('library-copy-start')
    expect(start).toHaveTextContent('Copy 4 items to Supplies')

    await user.click(start)
    expect(startedCopies).toEqual([
      { direction: 'bb_to_sb', file_ids: [500, SAME_HASH, FAILS], folder_ids: [20], target_folder_id: 3 },
    ])
    // The first read finds it running; the poll then finds it done.
    expect(await screen.findByTestId('library-copy-progress')).toHaveTextContent(/Copying…/)
    await waitFor(() => expect(screen.getByTestId('library-copy-progress')).toHaveTextContent(/Done: 1 copied, 1 already there, 1 failed/), {
      timeout: POLL_MS * 4,
    })
    expect(screen.getByTestId('library-copy-result-500')).toHaveTextContent('Copied')
    expect(screen.getByTestId(`library-copy-result-${SAME_HASH}`)).toHaveTextContent('Already there')
    expect(screen.getByTestId(`library-copy-result-${FAILS}`)).toHaveTextContent(/Failed.*no room/)
    expect(onCopied).toHaveBeenCalledTimes(1)
  })

  it("starts a ScadBuddy-to-Bambuddy copy from the page's ticked files, into a Bambuddy folder", async () => {
    const picked = libraryFiles.filter((file) => file.folder_id === 1)
    const { user } = renderDialog(picked)
    await screen.findByRole('tree', { name: 'Source folder' })
    await user.click(screen.getByTestId('library-copy-direction-sb_to_bb'))
    // The ticked files are the selection, listed from their own folder.
    const files = await screen.findByRole('list', { name: 'Source files' })
    for (const file of picked) expect(within(files).getByTestId(`library-copy-file-${file.id}`)).toBeChecked()
    const target = screen.getByRole('tree', { name: 'Target folder' })
    await user.click(await within(target).findByRole('treeitem', { name: 'Keychains' }))
    await user.click(screen.getByTestId('library-copy-start'))
    expect(startedCopies).toEqual([
      { direction: 'sb_to_bb', file_ids: picked.map((file) => file.id), folder_ids: [], target_folder_id: 22 },
    ])
    expect(await screen.findByTestId('library-copy-progress')).toBeInTheDocument()
  })

  it('shows a refusal from the start, with the selection kept', async () => {
    server.use(
      http.post('/api/v1/print/library-copies', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'no Bambuddy URL is configured; set one in Settings' }, { status: 409 }),
      ),
    )
    const { user } = renderDialog()
    await user.click(await screen.findByTestId('library-copy-file-503'))
    await user.click(screen.getByTestId('library-copy-start'))
    expect(await screen.findByRole('alert')).toHaveTextContent(/no Bambuddy URL/)
    expect(screen.getByTestId('library-copy-file-503')).toBeChecked()
    await act(async () => {})
  })
})
