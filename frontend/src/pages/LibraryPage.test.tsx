import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { useLocation } from 'react-router'
import { beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { api } from '../api/client'
import { libraryFiles } from '../mocks/features/library'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { NARROW_QUERY } from '../lib/useMediaQuery'
import { LibraryPage } from './LibraryPage'
import type * as TraceAction from '../lib/traceAction'

// Each test's actions only (#2892): `toContain` would otherwise pass on an earlier test's.
beforeEach(() => {
  actions.length = 0
})

/** The user actions started, by name (#2292): `traceAction` itself, recorded. */
const actions = vi.hoisted(() => [] as string[])
vi.mock('../lib/traceAction', async (importOriginal) => {
  const actual = await importOriginal<typeof TraceAction>()
  const traceAction: typeof actual.traceAction = (name, attributes, run) => {
    actions.push(name)
    return actual.traceAction(name, attributes, run)
  }
  return { ...actual, traceAction }
})

/**
 * Counts the listing GETs MSW answers from now on. A copy, move or rename reloads the
 * listing: the page empties it, then fills it from the answer, so a card read between
 * the action and the answer is the old one, about to be unmounted. Wait for this count
 * before reading the new listing.
 */
function listingAnswers(): { count: () => number; stop: () => void } {
  let answered = 0
  const listen = ({ request }: { request: Request }) => {
    if (request.method === 'GET' && new URL(request.url).pathname === '/api/v1/print/library') answered += 1
  }
  server.events.on('response:mocked', listen)
  return { count: () => answered, stop: () => server.events.removeListener('response:mocked', listen) }
}

/** The request body of each POST to `path`, recorded until `stop`. */
function posted(path: string): { bodies: unknown[]; stop: () => void } {
  const bodies: unknown[] = []
  const listen = async ({ request }: { request: Request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname === path) bodies.push(await request.clone().json())
  }
  server.events.on('request:start', listen)
  return { bodies, stop: () => server.events.removeListener('request:start', listen) }
}

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
    await user.click(within(await screen.findByTestId('library-file-89')).getByRole('checkbox', { name: /^Select bag-clip/ }))
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
    // #2284 — a plain file in the mix: where the result is filed is shown, by name.
    expect(within(dialog).getByLabelText('File under')).toHaveDisplayValue('Name Keychain')
    await user.click(within(dialog).getByRole('button', { name: 'Arrange' }))
    const done = await screen.findByRole('status', { name: 'Arranged' })
    expect(done).toHaveTextContent('Arranged onto 2 plates.')
    expect(done).toHaveTextContent("Saved in Name Keychain's history.")
    expect(screen.getByRole('link', { name: 'Open in History' })).toHaveAttribute('href', '/m/name-keychain/history')
    expect(screen.getByRole('button', { name: 'Arrange selected (0)' })).toBeDisabled()
    // #2287 — it is not sent anywhere yet; Print opens the print dialog with the result.
    const next = screen.getByRole('group', { name: 'Next step' })
    expect(next).toHaveTextContent('Not sent to Bambuddy yet')
    expect(within(next).getByRole('button', { name: 'Download' })).toBeInTheDocument()
    await user.click(within(next).getByRole('button', { name: 'Print' }))
    expect(await screen.findByRole('dialog', { name: 'Print' })).toBeInTheDocument()
  })

  it('Advanced lists the sliced file with Print and is remembered', async () => {
    const { user, unmount } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    const sliced = await screen.findByTestId('library-file-104')
    // #2671 — printed as it is, through the same dialog.
    expect(within(sliced).getByRole('button', { name: /^Print / })).toBeInTheDocument()
    expect(within(sliced).queryByText(/print it from Bambuddy/i)).toBeNull()
    unmount()
    renderPage(<LibraryPage />, { route: '/library' })
    expect(await screen.findByTestId('library-file-104')).toBeInTheDocument()
  })

  it('leaves a ticked sliced file out of Arrange selected (#2671)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    await user.click(await screen.findByRole('checkbox', { name: /Select .*\.gcode\.3mf/ }))
    expect(screen.getByRole('button', { name: 'Arrange selected (0)' })).toBeDisabled()
    await user.click(screen.getAllByRole('checkbox', { name: /Select .*\.3mf$/ })[0] as HTMLElement)
    expect(screen.getByRole('button', { name: 'Arrange selected (1)' })).toBeEnabled()
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

  it("reads the dialog's choices, plates, projects and options once per open (#2361)", async () => {
    const asked: string[] = []
    const analysed: Promise<unknown>[] = []
    const listen = ({ request }: { request: Request }) => {
      const url = new URL(request.url)
      asked.push(`${request.method} ${url.pathname}${url.search}`)
      if (url.pathname === '/api/v1/analyzers/run') analysed.push(request.clone().json())
    }
    server.events.on('request:start', listen)
    try {
      const { user } = renderPage(<LibraryPage />, { route: '/library' })
      await user.click(await screen.findByTestId('library-print-89'))
      const dialog = await screen.findByRole('dialog', { name: 'Print' })
      await within(dialog).findByTestId('filament-slot-1')
      await waitFor(() => expect(analysed.length).toBeGreaterThan(0))
      await waitFor(() => expect(within(dialog).getByRole('button', { name: /^Print$/ })).toBeEnabled())

      const count = (path: string) => asked.filter((line) => line === `GET ${path}`).length
      expect(count('/api/v1/print/library/89/choices')).toBe(1)
      expect(count('/api/v1/print/library/89/plates')).toBe(1)
      expect(count('/api/v1/print/projects')).toBe(1)
      expect(count('/api/v1/settings/print-options')).toBe(1)
      // The file the page listed is not read again to open its dialog.
      expect(asked.filter((line) => line.includes('file_id='))).toEqual([])
      // No analysis of an empty plan before the suggested spools are set.
      for (const body of await Promise.all(analysed)) {
        expect((body as { request: { filament_plan: { slots: unknown[] } } }).request.filament_plan.slots).not.toEqual([])
      }
    } finally {
      server.events.removeListener('request:start', listen)
    }
  })

  it('does not read the printed file on its own when the listing is read again (#2822)', async () => {
    const asked: string[] = []
    const listen = ({ request }: { request: Request }) => {
      const url = new URL(request.url)
      asked.push(`${request.method} ${url.pathname}${url.search}`)
    }
    server.events.on('request:start', listen)
    try {
      renderPage(<LibraryPage />, { route: '/library' })
      fireEvent.click(await screen.findByTestId('library-print-89'))
      await screen.findByRole('dialog', { name: 'Print' })
      const lists = () => asked.filter((line) => line.startsWith('GET /api/v1/print/library?')).length
      const before = lists()

      // Advanced empties the listing and reads it again, with the dialog still open.
      fireEvent.click(screen.getByTestId('library-advanced'))
      await waitFor(() => expect(lists()).toBeGreaterThan(before))
      await screen.findByTestId('library-file-104')

      expect(asked.filter((line) => line.includes('file_id='))).toEqual([])
    } finally {
      server.events.removeListener('request:start', listen)
    }
  })

  it('prints a file in Simple mode under the last project printed to (#768)', async () => {
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json({ projects: fixtures.projectViews, last_project_id: 2 }),
      ),
    )
    const run = vi.spyOn(api, 'runLibraryPrint')
    // MSW's answers to the run's POST, the choices it remembers and the progress read
    // the sent dialog makes, counted until the test ends (see below).
    const answered: string[] = []
    const onAnswer = ({ request }: { request: Request }) => {
      answered.push(`${request.method} ${new URL(request.url).pathname}`)
    }
    server.events.on('response:mocked', onAnswer)
    onTestFinished(() => {
      server.events.removeListener('response:mocked', onAnswer)
    })
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
    // Not ended with a request in flight: unmounting aborts it, and in CI the next
    // test's listing read then bypassed MSW ("could not reach its server") and never
    // showed its loading state (the #2944 shape). The run's POST, the choices it
    // remembers, then the sent dialog's first progress read; its next is seconds away.
    await waitFor(() => {
      expect(answered).toContain('POST /api/v1/print/library/89/run')
      expect(answered).toContain('PUT /api/v1/print/library/89/choices')
      expect(answered).toContain('GET /api/v1/print/library/89/progress')
    })
    run.mockRestore()
  })

  it('clears the old listing while a new folder is loading, so its Print buttons go away', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-print-89')

    server.use(http.get('/api/v1/print/library', () => delay('infinite')))
    await user.click(screen.getByTestId('library-folder-9'))

    expect(await screen.findByText(/Reading the Bambuddy library/)).toBeInTheDocument()
    expect(screen.queryByTestId('library-print-89')).toBeNull()
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

    const bulk = within(tree).getByRole('treeitem', { name: 'Bulk 300 files' })
    await user.click(bulk)
    expect(bulk).toHaveAttribute('aria-selected', 'true')
    expect(within(tree).getByRole('treeitem', { name: 'Top level' })).toHaveAttribute('aria-selected', 'false')
    expect(within(tree).getByRole('treeitem', { name: 'MakerWorld 2 files' })).toBeInTheDocument()
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

  it('on a phone, folds the folder tree behind a button naming the open folder (#2368)', async () => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query === NARROW_QUERY,
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
    }))
    try {
      const { user } = renderPage(<><LibraryPage /><Where /></>, { route: '/library/Spec/MakerWorld/Work' })
      expect(await screen.findByTestId('library-file-120')).toBeInTheDocument()
      // The files come first: no tree until it is asked for.
      expect(screen.queryByRole('tree', { name: 'Library folders' })).toBeNull()
      const toggle = screen.getByRole('button', { name: /^Folder: Spec \/ MakerWorld \/ Work/ })
      expect(toggle).toHaveAttribute('aria-expanded', 'false')

      await user.click(toggle)
      expect(toggle).toHaveAttribute('aria-expanded', 'true')
      const tree = screen.getByRole('tree', { name: 'Library folders' })
      expect(toggle).toHaveAttribute('aria-controls', tree.parentElement?.id)
      await user.click(within(tree).getByRole('treeitem', { name: 'Top level' }))

      // Choosing a folder folds the tree away again, and focus goes back to the button
      // rather than falling to the page (#3315).
      expect(screen.getByTestId('where')).toHaveTextContent(/^\/library$/)
      expect(screen.queryByRole('tree', { name: 'Library folders' })).toBeNull()
      const folded = await screen.findByRole('button', { name: /^Folder: Top level/ })
      expect(folded).toHaveAttribute('aria-expanded', 'false')
      expect(folded).toHaveFocus()
      // What it controls is there while folded too, only hidden.
      const controls = document.getElementById(folded.getAttribute('aria-controls') ?? '')
      expect(controls).not.toBeNull()
      expect(controls).not.toBeVisible()
    } finally {
      vi.unstubAllGlobals()
    }
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
    // Each a user action of its own in Tempo (#2292).
    expect(actions).toEqual(expect.arrayContaining(['library_delete', 'library_restore']))
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

  it('moves a file from its actions menu, keeping it under its id (#2670)', async () => {
    const move = posted('/api/v1/print/library/move')
    onTestFinished(move.stop)
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await user.click((await screen.findAllByRole('button', { name: /^More actions for bag-clip/ }))[0]!)
    await user.click(within(screen.getByRole('menu')).getByRole('menuitem', { name: 'Move to…' }))
    const dialog = await screen.findByRole('dialog', { name: /^Move bag-clip.*to…$/ })
    // It starts on the folder the file is in, which a move cannot go to.
    expect(within(dialog).getByTestId('library-move-staying')).toHaveTextContent('It is in Top level already.')
    expect(within(dialog).getByTestId('library-move-confirm')).toBeDisabled()
    await user.click(within(dialog).getByTestId('library-move-folder-1'))
    // MakerWorld is no project's folder, and the file was in none: nothing to warn of.
    expect(within(dialog).queryByTestId('library-move-project-loss')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Move to MakerWorld' }))
    const toast = await screen.findByTestId('library-done')
    expect(toast).toHaveTextContent('Moved 1 file to MakerWorld.')
    expect(move.bodies).toEqual([{ file_ids: [89], folder_id: 1 }])
    await waitFor(() => expect(screen.queryByTestId('library-file-89')).toBeNull())
    await user.click(screen.getByTestId('library-folder-1'))
    expect(await screen.findByTestId('library-file-89')).toBeInTheDocument()
  })

  it("warns before a move takes a file out of a project's folder, and says so after (#2670)", async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library/Lamp' })
    await user.click(await screen.findByRole('checkbox', { name: 'Select lamp-shade.3mf' }))
    await user.click(screen.getByRole('button', { name: 'Move selected (1)' }))
    const dialog = await screen.findByRole('dialog', { name: 'Move lamp-shade.3mf to…' })
    expect(within(dialog).queryByTestId('library-move-project-loss')).toBeNull()
    await user.click(within(dialog).getByTestId('library-move-folder-root'))
    expect(within(dialog).getByTestId('library-move-project-loss')).toHaveTextContent(
      'The project Desk lamp will lose lamp-shade.3mf',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Move to Top level' }))
    const toast = await screen.findByTestId('library-done')
    expect(toast).toHaveTextContent('Moved 1 file to Top level.')
    expect(toast).toHaveTextContent('The project Desk lamp no longer lists lamp-shade.3mf.')
    // The move's reload has answered before the test ends.
    expect(await screen.findByText('No files here yet.')).toBeInTheDocument()
  })

  it('copies the selection into a nested folder, and reports a skipped move (#2670)', async () => {
    const copy = posted('/api/v1/print/library/copy')
    onTestFinished(copy.stop)
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    for (const card of ['library-file-89', 'library-file-91']) {
      await user.click(within(screen.getByTestId(card)).getByRole('checkbox'))
    }
    await user.click(screen.getByRole('button', { name: 'Copy selected (2)' }))
    const dialog = await screen.findByRole('dialog', { name: 'Copy 2 files to…' })
    await user.click(within(dialog).getByTestId('library-copy-folder-toggle-3'))
    await user.click(within(dialog).getByTestId('library-copy-folder-4'))
    const reloads = listingAnswers()
    onTestFinished(reloads.stop)
    await user.click(within(dialog).getByRole('button', { name: 'Copy to Supplies / Storage' }))
    expect(await screen.findByTestId('library-done')).toHaveTextContent('Copied 2 files to Supplies / Storage.')
    expect(copy.bodies).toEqual([{ file_ids: [89, 91], folder_id: 4 }])
    // The originals stay where they were, in the listing the copy reloaded.
    await waitFor(() => expect(reloads.count()).toBe(1))
    await waitFor(() => expect(screen.getByTestId('library-file-89')).toBeInTheDocument())
  })

  it('reports a file Bambuddy would not move (#2670)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library/Spec/MakerWorld/Work' })
    await user.click(await screen.findByRole('checkbox', { name: 'Select alex-headphone-hook.3mf' }))
    await user.click(screen.getByRole('button', { name: 'Move selected (1)' }))
    const dialog = await screen.findByRole('dialog', { name: 'Move alex-headphone-hook.3mf to…' })
    await user.click(within(dialog).getByTestId('library-move-folder-root'))
    const reloads = listingAnswers()
    onTestFinished(reloads.stop)
    await user.click(within(dialog).getByRole('button', { name: 'Move to Top level' }))
    const toast = await screen.findByTestId('library-done')
    expect(toast).toHaveTextContent('Nothing was moved.')
    expect(within(toast).getByTestId('library-done-skipped')).toHaveTextContent('alex-headphone-hook.3mf')
    // The file Bambuddy skipped is still listed once the move's reload has answered.
    await waitFor(() => expect(reloads.count()).toBe(1))
    await waitFor(() => expect(screen.getByTestId('library-file-121')).toBeInTheDocument())
  })

  it('renames a file, keeping its extension, and shows a name Bambuddy refuses (#2670)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library/MakerWorld' })
    await user.click(await screen.findByRole('button', { name: "More actions for Clara's Wand.3mf" }))
    await user.click(within(screen.getByRole('menu')).getByRole('menuitem', { name: 'Rename…' }))
    const dialog = await screen.findByRole('dialog', { name: "Rename Clara's Wand.3mf" })
    const input = within(dialog).getByRole('textbox', { name: 'New name' })
    expect(input).toHaveValue("Clara's Wand")
    expect(within(dialog).getByRole('button', { name: 'Rename' })).toBeDisabled()
    await user.clear(input)
    await user.type(input, 'Wand?')
    await user.click(within(dialog).getByRole('button', { name: 'Rename' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent("printer's SD card")
    await user.clear(input)
    await user.type(input, 'Wand for Clara{Enter}')
    expect(await screen.findByTestId('library-done')).toHaveTextContent(
      "Renamed Clara's Wand.3mf to Wand for Clara.3mf.",
    )
    // Waited for as a whole: the rename reloads the listing, which unmounts the card
    // until the reload answers, so a card got before then never shows the new name.
    await waitFor(() =>
      expect(within(screen.getByTestId('library-file-67')).getByTitle('Wand for Clara.3mf')).toBeInTheDocument(),
    )
  })
})

function Where() {
  const location = useLocation()
  return <p data-testid="where">{`${location.pathname}${location.search}`}</p>
}
