import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
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

  it('marks the open folder, and reads its count apart from its name', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    const nav = screen.getByRole('navigation', { name: 'Library folders' })
    expect(within(nav).getByRole('button', { name: 'Top level' })).toHaveAttribute('aria-current', 'true')

    const bulk = within(nav).getByRole('button', { name: 'Bulk, 300 files' })
    await user.click(bulk)
    expect(bulk).toHaveAttribute('aria-current', 'true')
    expect(within(nav).getByRole('button', { name: 'Top level' })).not.toHaveAttribute('aria-current')
    expect(within(nav).getByRole('button', { name: 'MakerWorld, 1 file' })).toBeInTheDocument()
  })
})
