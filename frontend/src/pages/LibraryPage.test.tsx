import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { libraryFiles } from '../mocks/features/library'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { LibraryPage } from './LibraryPage'

describe('LibraryPage', () => {
  beforeEach(() => {
    resetMockState()
    window.localStorage.clear()
  })

  it('lists the root 3MFs with Print, and hides the sliced file', async () => {
    renderPage(<LibraryPage />, { route: '/library' })
    const card = await screen.findByTestId('library-file-89')
    expect(within(card).getByRole('button', { name: /^Print / })).toBeInTheDocument()
    expect(screen.queryByTestId('library-file-104')).toBeNull()
    expect(screen.getByText(/1 more under Advanced/)).toBeInTheDocument()
  })

  it('arranges the files ticked, made by ScadBuddy or not (#1864, #1863)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    const arrange = await screen.findByRole('button', { name: 'Arrange selected (0)' })
    expect(arrange).toBeDisabled()
    await user.click(within(screen.getByTestId('library-file-89')).getByRole('checkbox', { name: /^Select bag-clip/ }))
    // Picks are kept across folders: one file here, one in MakerWorld.
    await user.click(screen.getByTestId('library-folder-1'))
    await user.click(await screen.findByRole('checkbox', { name: "Select Clara's Wand.3mf" }))
    // Picks out of sight can all be dropped at once.
    await user.click(screen.getByRole('button', { name: 'Clear selection' }))
    expect(screen.getByRole('button', { name: 'Arrange selected (0)' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: "Select Clara's Wand.3mf" }))
    await user.click(screen.getByTestId('library-folder-root'))
    await user.click(
      within(await screen.findByTestId('library-file-89')).getByRole('checkbox', { name: /^Select bag-clip/ }),
    )
    await user.click(screen.getByRole('button', { name: 'Arrange selected (2)' }))
    const dialog = await screen.findByRole('dialog', { name: 'Arrange' })
    expect(await within(dialog).findByLabelText('Copies of wall — Reagan')).toHaveValue(2)
    expect(await within(dialog).findByLabelText("Copies of Wand — Clara's Wand.3mf")).toHaveValue(1)
    await user.click(within(dialog).getByRole('button', { name: 'Arrange' }))
    expect(await screen.findByRole('status', { name: 'Arranged' })).toHaveTextContent('Arranged onto 2 plates.')
    expect(screen.getByRole('link', { name: 'Open in History' })).toHaveAttribute('href', '/m/name-keychain/history')
    expect(screen.getByRole('button', { name: 'Arrange selected (0)' })).toBeDisabled()
  })

  it('Advanced lists the sliced file without Print and is remembered', async () => {
    const { user, unmount } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    const sliced = await screen.findByTestId('library-file-104')
    expect(within(sliced).queryByRole('button', { name: /^Print / })).toBeNull()
    expect(within(sliced).getByText(/print it from Bambuddy/i)).toBeInTheDocument()
    unmount()
    renderPage(<LibraryPage />, { route: '/library' })
    expect(await screen.findByTestId('library-file-104')).toBeInTheDocument()
  })

  it('renders a folder of hundreds of files with lazy thumbnails', async () => {
    const { user, container } = renderPage(<LibraryPage />, { route: '/library' })
    await user.click(await screen.findByTestId('library-folder-9'))
    await waitFor(() => expect(screen.getAllByTestId(/^library-file-/)).toHaveLength(300))
    // The thumbnails are decorative (alt=""): the caption names the file.
    const images = Array.from(container.querySelectorAll('img'))
    expect(images).toHaveLength(300)
    expect(images.every((image) => image.getAttribute('loading') === 'lazy')).toBe(true)
  })

  it('still toggles Advanced when the browser refuses storage', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    try {
      const { user } = renderPage(<LibraryPage />, { route: '/library' })
      await screen.findByTestId('library-file-89')
      await user.click(screen.getByTestId('library-advanced'))
      expect(await screen.findByTestId('library-file-104')).toBeInTheDocument()
      expect(setItem).toHaveBeenCalledWith('scadbuddy.library.advanced', '1')
    } finally {
      getItem.mockRestore()
      setItem.mockRestore()
    }
  })

  it('opens the Print dialog on the file', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await user.click(await screen.findByTestId('library-print-89'))
    expect(await screen.findByRole('dialog', { name: 'Print' })).toBeInTheDocument()
  })

  it('prints a file in Simple mode under the last project printed to (#768)', async () => {
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json({ projects: fixtures.projectViews, last_project_id: 2 }),
      ),
    )
    const run = vi.spyOn(api, 'runLibraryPrint')
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await user.click(await screen.findByTestId('library-print-89'))
    const dialog = await screen.findByRole('dialog', { name: 'Print' })
    await within(dialog).findByTestId('filament-slot-1')

    expect(within(dialog).queryByTestId('project-select')).toBeNull()
    const print = within(dialog).getByRole('button', { name: /^Print$/ })
    await waitFor(() => expect(print).toBeEnabled())
    await user.click(print)
    await waitFor(() =>
      expect(run).toHaveBeenCalledWith(
        89,
        expect.objectContaining({ project_id: 2 }),
        expect.any(AbortSignal),
        expect.any(Function),
      ),
    )
    run.mockRestore()
  })

  it('clears the old listing while a new folder is loading, so its Print buttons go away', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-print-89')

    server.use(http.get('/api/v1/print/library', () => delay('infinite')))
    await user.click(screen.getByTestId('library-folder-9'))

    await waitFor(() => expect(screen.queryByTestId('library-print-89')).toBeNull())
    expect(screen.getByText(/Reading the Bambuddy library/)).toBeInTheDocument()
  })

  it('cuts a name with an early dot in the middle, keeping only its end whole (#935)', async () => {
    const filename = 'v1.2_shelf_bracket_with_cable_channel_and_a_long_tail.3mf'
    server.use(
      http.get('/api/v1/print/library', () =>
        HttpResponse.json({
          folder_id: null,
          all: false,
          folders: [],
          files: [{ ...libraryFiles[0], id: 7001, filename, file_type: '3mf', folder_id: null }],
          hidden: 0,
        }),
      ),
    )
    renderPage(<LibraryPage />, { route: '/library' })
    const card = await screen.findByTestId('library-file-7001')
    const caption = card.querySelector(`p[title="${filename}"]`)
    const parts = Array.from(caption?.querySelectorAll('span') ?? [], (span) => span.textContent)
    expect(parts.join('')).toBe(filename)
    expect(parts).toHaveLength(2)
    expect(parts[1]).toBe('ong_tail.3mf')
  })

  it('says when the library cannot be read', async () => {
    server.use(
      http.get('/api/v1/print/library', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/bambuddy-unavailable', title: 'Bad Gateway', status: 502, detail: 'could not reach Bambuddy to list the library files: ConnectError' },
          { status: 502 },
        ),
      ),
    )
    renderPage(<LibraryPage />, { route: '/library' })
    expect(await screen.findByRole('alert')).toHaveTextContent('could not reach Bambuddy')
  })
})

describe('LibraryPage, item context (#975)', () => {
  beforeEach(() => {
    resetMockState()
    window.localStorage.clear()
  })

  it("names each card's Print after its file", async () => {
    renderPage(<LibraryPage />, { route: '/library' })
    const card = await screen.findByTestId('library-file-89')
    const filename = card.querySelector('p')?.textContent ?? ''
    expect(filename).not.toBe('')
    expect(within(card).getByRole('button', { name: `Print ${filename}` })).toBeInTheDocument()
  })

  it('links each printable file to its own print history (#1755)', async () => {
    renderPage(<LibraryPage />, { route: '/library' })
    const card = await screen.findByTestId('library-file-89')
    const filename = card.querySelector('p')?.textContent ?? ''
    expect(within(card).getByRole('link', { name: `Prints of ${filename}` })).toHaveAttribute(
      'href',
      '/prints?file=89',
    )
  })

  it('marks the open folder in a tree, and reads its count apart from its name', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    const tree = screen.getByRole('tree', { name: 'Library folders' })
    expect(within(tree).getByRole('treeitem', { name: 'Top level' })).toHaveAttribute('aria-selected', 'true')

    const bulk = within(tree).getByRole('treeitem', { name: 'Bulk, 300 files' })
    await user.click(bulk)
    expect(bulk).toHaveAttribute('aria-selected', 'true')
    expect(within(tree).getByRole('treeitem', { name: 'Top level' })).toHaveAttribute('aria-selected', 'false')
    expect(within(tree).getByRole('treeitem', { name: 'MakerWorld, 2 files' })).toBeInTheDocument()
  })

  it('opens and closes folders with the keyboard, others starting closed (#2165)', async () => {
    const { user } = renderPage(<><LibraryPage /><Where /></>, { route: '/library' })
    await screen.findByTestId('library-file-89')
    const spec = screen.getByTestId('library-folder-10')
    expect(spec).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('library-folder-11')).toBeNull()

    screen.getByRole('treeitem', { name: 'Top level' }).focus()
    await user.keyboard('{End}')
    expect(spec).toHaveFocus()
    await user.keyboard('{ArrowRight}')
    expect(spec).toHaveAttribute('aria-expanded', 'true')
    await user.keyboard('{ArrowRight}')
    const makerWorld = screen.getByTestId('library-folder-11')
    expect(makerWorld).toHaveFocus()
    expect(makerWorld).toHaveAttribute('aria-level', '2')
    await user.keyboard('{ArrowRight}{ArrowDown}')
    expect(screen.getByTestId('library-folder-12')).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.getByTestId('where')).toHaveTextContent('/library/Spec/MakerWorld/Work')
    expect(await screen.findByTestId('library-file-120')).toBeInTheDocument()
    await user.keyboard('{ArrowLeft}')
    expect(makerWorld).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(makerWorld).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('library-folder-12')).toBeNull()
  })

  it('opens a deep link on its folder, expanded to it (#2165)', async () => {
    renderPage(<LibraryPage />, { route: '/library/Spec/MakerWorld/Work' })
    expect(await screen.findByTestId('library-file-120')).toBeInTheDocument()
    expect(screen.getByTestId('library-folder-10')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('library-folder-12')).toHaveAttribute('aria-selected', 'true')
    // The other MakerWorld, at the top, stays where it is.
    expect(screen.getByTestId('library-folder-1')).toHaveAttribute('aria-selected', 'false')
  })

  it('finds a linked file in its folder and highlights it, and a print link opens the dialog', async () => {
    renderPage(<><LibraryPage /><Where /></>, { route: '/library?print=67&file=67' })
    const card = await screen.findByTestId('library-file-67')
    expect(card).toHaveAttribute('aria-current', 'true')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/library/MakerWorld?print=67&file=67'))
    expect(await screen.findByRole('dialog', { name: 'Print' })).toBeInTheDocument()
  })

  it('says so when the path names no folder', async () => {
    renderPage(<LibraryPage />, { route: '/library/Nowhere' })
    expect(await screen.findByRole('alert')).toHaveTextContent('There is no folder Nowhere in the library.')
  })

  it('deletes a file to the trash after a confirmation, and Undo restores it (#2167)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library/Spec/MakerWorld/Work' })
    await user.click(await screen.findByRole('button', { name: 'Delete drawer-label.3mf' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete drawer-label.3mf?' })
    expect(dialog).toHaveTextContent("It goes to Bambuddy's trash.")
    await user.click(within(dialog).getByRole('button', { name: 'Delete file' }))
    const toast = await screen.findByTestId('library-deleted')
    expect(toast).toHaveTextContent("Moved 1 file to Bambuddy's trash.")
    await waitFor(() => expect(screen.queryByTestId('library-file-120')).toBeNull())

    await user.click(within(toast).getByRole('button', { name: 'Undo' }))
    expect(await within(toast).findByText('Restored 1 file.')).toBeInTheDocument()
    expect(await screen.findByTestId('library-file-120')).toBeInTheDocument()
  })

  it('deletes the selection, warns of an external file, and reports the one skipped (#2167)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library/Spec/MakerWorld/Work' })
    await screen.findByTestId('library-file-120')
    for (const name of ['drawer-label.3mf', 'alex-headphone-hook.3mf', 'nas-share-bracket.3mf']) {
      await user.click(screen.getByRole('checkbox', { name: `Select ${name}` }))
    }
    await user.click(screen.getByRole('button', { name: 'Delete selected (3)' }))
    const dialog = await screen.findByRole('dialog', { name: 'Delete 3 files?' })
    expect(within(dialog).getByRole('list', { name: 'Files to delete' })).toHaveTextContent('nas-share-bracket.3mf')
    expect(within(dialog).getByTestId('library-delete-external')).toHaveTextContent(
      'nas-share-bracket.3mf is linked from an external folder',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Delete 3 files' }))
    const toast = await screen.findByTestId('library-deleted')
    expect(toast).toHaveTextContent("Moved 1 file to Bambuddy's trash. Removed 1 external file for good.")
    expect(within(toast).getByTestId('library-skipped')).toHaveTextContent('alex-headphone-hook.3mf')
    // The skipped file stays selected and listed; the deleted ones leave the selection.
    expect(await screen.findByRole('button', { name: 'Delete selected (1)' })).toBeEnabled()
    await waitFor(() => expect(screen.queryByTestId('library-file-122')).toBeNull())
    expect(screen.getByTestId('library-file-121')).toBeInTheDocument()
  })
})

function Where() {
  const location = useLocation()
  return <p data-testid="where">{`${location.pathname}${location.search}`}</p>
}
