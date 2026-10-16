import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { useLocation } from 'react-router'
import { beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { api } from '../api/client'
import { libraryFiles, libraryFolders } from '../mocks/features/library'
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
    expect(within(dialog).getByLabelText('File under')).toHaveTextContent('Name Keychain')
    await user.click(within(dialog).getByRole('button', { name: 'Arrange' }))
    const done = await screen.findByRole('status', { name: 'Arranged' })
    expect(done).toHaveTextContent('Arranged onto 2 plates.')
    // #2286 — unnamed, it is saved under a name made of what it holds.
    expect(done).toHaveTextContent("Saved as Arrange of Name Keychain, Clara's Wand in Name Keychain's history.")
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

  it('offers a batch kept for a file still being checked, to resume (#2718 review)', async () => {
    const batch = await api.createPrintBatch([
      { library_file_id: 89, filename: 'a.3mf', sliced: false },
      { library_file_id: 91, filename: 'b.3mf', sliced: false },
    ])
    const pending = { status: 'pending', run_id: null, detail: null, attempt: 0 }
    server.use(
      http.get('/api/v1/print/batches', () =>
        HttpResponse.json([{ ...batch, items: [batch.items[0], { ...batch.items[1], outcome: pending }] }]),
      ),
    )
    const { user } = renderPage(<><LibraryPage /><Where /></>, { route: '/library' })

    const offer = await screen.findByTestId(`library-kept-batch-${batch.id}`)
    expect(offer).toHaveTextContent('A batch print of 2 files has a file still being checked.')
    await user.click(within(offer).getByRole('button', { name: 'Resume it' }))

    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(`?batch=${batch.id}`))
    expect(screen.queryByTestId(`library-kept-batch-${batch.id}`)).toBeNull()
  })

  it('prints the ticked files as one batch, and a reload resumes it (#2672)', async () => {
    const { user, unmount } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    const print = screen.getByTestId('library-print-selected')
    expect(print).toBeDisabled()
    const boxes = screen.getAllByRole('checkbox', { name: /^Select / })
    await user.click(boxes[0] as HTMLElement)
    expect(print).toBeDisabled()
    await user.click(boxes[1] as HTMLElement)
    expect(print).toHaveTextContent('Print 2 files')
    const created = vi.spyOn(api, 'createPrintBatch')
    await user.click(print)
    expect(await screen.findByRole('dialog', { name: /^File 1 of 2 — / })).toBeInTheDocument()
    const batch = await (created.mock.results[0]?.value as ReturnType<typeof api.createPrintBatch>)
    await api.putPrintBatchStep(batch.id, 1)
    unmount()

    renderPage(<LibraryPage />, { route: `/library?batch=${batch.id}` })
    expect(await screen.findByRole('dialog', { name: /^File 2 of 2 — / })).toBeInTheDocument()
  })

  it("prints a project's file list as one batch, from the folder linked to it (#2724)", async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    const folder = await screen.findByTestId('library-folder-14')
    // The visible name alone (#2879); the count and the project are its description.
    expect(folder).toHaveAccessibleName('Raegan')
    expect(folder).toHaveAccessibleDescription('3 files, project Reagan Keychain')
    await user.click(folder)
    await screen.findByTestId('library-file-140')
    expect(screen.getByTestId('library-project')).toHaveTextContent('Select all in folder, then Print')
    // Without Advanced the sliced file is not listed, so not selected either.
    await user.click(screen.getByTestId('library-select-all'))
    expect(screen.getByTestId('library-select-all')).toBeDisabled()
    await user.click(screen.getByTestId('library-print-selected'))
    expect(await screen.findByRole('dialog', { name: /^File 1 of 2 — keychain-reagan\.3mf$/ })).toBeInTheDocument()
    expect(screen.getByTestId('batch-strip')).toHaveTextContent('keychain-reagan-2.3mf')
  })

  it('selects every listed printable file, sliced ones too under Advanced (#2724)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library?project=1' })
    await screen.findByTestId('library-file-140')
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    await screen.findByTestId('library-file-142')
    // It says what it adds: this folder's unticked files (#2757 review).
    expect(screen.getByTestId('library-select-all')).toHaveTextContent('Select all in folder (3)')
    await user.click(screen.getByTestId('library-select-all'))
    expect(screen.getByTestId('library-print-selected')).toHaveTextContent('Print 3 files')
    expect(screen.getByTestId('library-select-all')).toHaveTextContent('Select all in folder (0)')
  })

  it('adds each file once, however fast Select all in folder is pressed (#2757 review)', async () => {
    renderPage(<LibraryPage />, { route: '/library?project=1' })
    await screen.findByTestId('library-file-140')
    const button = screen.getByTestId('library-select-all')
    // Both presses before a render: the second sees the first one's files as unticked.
    act(() => {
      button.click()
      button.click()
    })
    expect(await screen.findByTestId('library-print-selected')).toHaveTextContent('Print 2 files')
  })

  it("names the project on the tree's marker, and gives no Select all hint in an empty project folder (#3112, #3113)", async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    const spares = await screen.findByTestId('library-folder-15')
    expect(within(spares).getByTitle('Project Reagan Keychain')).toHaveTextContent('project')
    await user.click(spares)
    await waitFor(() => expect(screen.getByTestId('library-select-all')).toBeDisabled())
    expect(screen.queryByTestId('library-project')).toBeNull()
    await user.click(screen.getByTestId('library-folder-14'))
    await screen.findByTestId('library-file-140')
    expect(screen.getByTestId('library-project')).toHaveTextContent('Select all in folder, then Print')
  })

  it('says a ticked sliced file is left out of Arrange (#2286)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    await user.click(screen.getByRole('switch', { name: 'Advanced' }))
    await user.click(
      within(await screen.findByTestId('library-file-89')).getByRole('checkbox', { name: /^Select bag-clip/ }),
    )
    await user.click(within(await screen.findByTestId('library-file-104')).getByRole('checkbox'))
    expect(screen.getByRole('button', { name: 'Arrange selected (1)' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Delete selected (2)' })).toBeEnabled()
    expect(screen.getByRole('note')).toHaveTextContent(
      "1 selected file can't be arranged: only unsliced 3MF and STL files can.",
    )
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

  it("shows a name whole on two lines, with ScadBuddy's suffixes and a slice as badges (#3634)", async () => {
    const copy = 'tfic-creeper-keychain-long (ScadBuddy) (2).3mf'
    const sliced = 'tfic-creeper-keychain-long (ScadBuddy).gcode.3mf'
    server.use(
      http.get('/api/v1/print/library', () =>
        HttpResponse.json({
          folder_id: null,
          all: true,
          folders: [],
          files: [
            { ...libraryFiles[0], id: 7001, filename: copy, file_type: '3mf', folder_id: null },
            { ...libraryFiles[0], id: 7002, filename: sliced, file_type: 'gcode.3mf', sliced: true, folder_id: null },
          ],
          hidden: 0,
        }),
      ),
    )
    renderPage(<LibraryPage />, { route: '/library' })
    const card = await screen.findByTestId('library-file-7001')
    expect(within(card).getByTestId('library-name-7001')).toHaveTextContent(/^tfic-creeper-keychain-long$/)
    expect(within(card).getByText('ScadBuddy copy')).toBeInTheDocument()
    expect(within(card).getByText('Copy 2')).toBeInTheDocument()
    expect(within(card).getByTestId('library-name-7001')).toHaveAttribute('data-filename', copy)
    const slicedCard = screen.getByTestId('library-file-7002')
    expect(within(slicedCard).getByText('Sliced')).toBeInTheDocument()
    expect(within(slicedCard).queryByText(/Copy/)).toBeNull()
  })

  it('finds a file by name in every folder, and keeps the search in the URL (#3630)', async () => {
    const { user } = renderPage(<><LibraryPage /><Where /></>, { route: '/library' })
    await screen.findByTestId('library-file-89')

    await user.type(screen.getByRole('searchbox', { name: 'Search the library' }), 'headphone')

    const result = await screen.findByTestId('library-file-121')
    expect(screen.getByTestId('library-search-summary')).toHaveTextContent('1 file matches “headphone”.')
    expect(screen.queryByTestId('library-file-89')).toBeNull()
    expect(screen.getByTestId('where')).toHaveTextContent('/library?q=headphone')
    const where = within(result).getByTestId('library-result-folder-121')
    expect(where).toHaveTextContent('In Spec / MakerWorld / Work')

    // Its folder opens on the file; Back returns to the results.
    await user.click(where)
    expect(screen.getByTestId('where')).toHaveTextContent('/library/Spec/MakerWorld/Work?file=121')
    expect(await screen.findByTestId('library-file-120')).toBeInTheDocument()
    expect(screen.getByRole('searchbox', { name: 'Search the library' })).toHaveValue('')
  })

  it('opens on the search the URL names, and says what only Advanced lists (#3630)', async () => {
    renderPage(<LibraryPage />, { route: '/library?q=bag%20clip' })
    const summary = await screen.findByTestId('library-search-summary')
    expect(summary).toHaveTextContent('2 files match “bag clip”.')
    expect(summary).toHaveTextContent('1 more under Advanced.')
    expect(screen.getByRole('searchbox', { name: 'Search the library' })).toHaveValue('bag clip')
  })

  it('keeps the last results under an error, with Retry (#3630, #3633)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library?q=headphone' })
    await screen.findByTestId('library-file-121')
    server.use(
      http.get('/api/v1/print/library/search', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/bambuddy-unavailable', title: 'Bad Gateway', status: 502, detail: 'could not reach Bambuddy to list every library file: ConnectError' },
          { status: 502 },
        ),
      ),
    )
    await user.type(screen.getByRole('searchbox', { name: 'Search the library' }), 's')
    expect(await screen.findByRole('alert')).toHaveTextContent('could not reach Bambuddy')
    expect(screen.getByTestId('library-file-121')).toBeInTheDocument()
    expect(screen.getByTestId('library-search-retry')).toBeInTheDocument()
  })

  it('asks for a fresh search after a delete, until one has answered (#3630)', async () => {
    const asked: string[] = []
    const listen = ({ request }: { request: Request }) => {
      const url = new URL(request.url)
      if (url.pathname === '/api/v1/print/library/search') asked.push(url.searchParams.get('fresh') ?? 'false')
    }
    server.events.on('request:start', listen)
    onTestFinished(() => server.events.removeListener('request:start', listen))
    const { user } = renderPage(<LibraryPage />, { route: '/library?q=bag%20clip' })
    await user.click(await screen.findByTestId('library-delete-89'))
    await user.click(await screen.findByTestId('library-delete-confirm'))

    await waitFor(() => expect(screen.queryByTestId('library-file-89')).toBeNull())
    expect(asked[0]).toBe('false')
    expect(asked.at(-1)).toBe('true')
    // Answered, the next search reads the kept copy again.
    await user.type(screen.getByRole('searchbox', { name: 'Search the library' }), ' 3')
    await waitFor(() => expect(asked.at(-1)).toBe('false'))
  })

  it('prints a search result from its card (#3630)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library?q=lamp' })
    await user.click(await screen.findByTestId('library-print-130'))
    expect(await screen.findByRole('dialog', { name: 'Print' })).toBeInTheDocument()
  })

  it('lists the subfolders of a folder that holds only folders, and opens one (#3631)', async () => {
    const { user } = renderPage(<><LibraryPage /><Where /></>, { route: '/library/Spec' })
    const folder = await screen.findByTestId('library-subfolder-11')
    expect(folder).toHaveTextContent('MakerWorld')
    expect(folder).toHaveTextContent('1 folder')
    expect(screen.queryByText(/No files here/)).toBeNull()
    // The open folder is expanded in the tree, so its subfolders show there too.
    expect(screen.getByTestId('library-folder-10')).toHaveAttribute('aria-expanded', 'true')

    await user.click(folder)
    expect(screen.getByTestId('where')).toHaveTextContent('/library/Spec/MakerWorld')
    expect(await screen.findByTestId('library-subfolder-12')).toHaveTextContent('3 files')
  })

  it('offers Retry when the library cannot be read, and reads it again (#3633)', async () => {
    let failing = true
    server.use(
      http.get('/api/v1/print/library', () =>
        failing
          ? HttpResponse.json(
              { type: 'https://scadbuddy.dev/problems/bambuddy-unavailable', title: 'Gateway Timeout', status: 504, detail: 'Bambuddy took too long to list the library files' },
              { status: 504 },
            )
          : undefined,
      ),
    )
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByRole('alert')
    failing = false
    await user.click(screen.getByTestId('library-retry'))
    expect(await screen.findByTestId('library-file-89')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows a folder opened before at once, while it is read again (#3633)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    await user.click(screen.getByTestId('library-folder-1'))
    await screen.findByTestId('library-file-67')

    server.use(http.get('/api/v1/print/library', () => delay('infinite')))
    await user.click(screen.getByTestId('library-folder-root'))

    expect(await screen.findByTestId('library-file-89')).toBeInTheDocument()
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
    const filename = card.querySelector('[data-filename]')?.getAttribute('data-filename') ?? ''
    expect(filename).not.toBe('')
    expect(within(card).getByRole('button', { name: `Print ${filename}` })).toBeInTheDocument()
  })

  it('links each printable file to its own print history (#1755)', async () => {
    renderPage(<LibraryPage />, { route: '/library' })
    const card = await screen.findByTestId('library-file-89')
    const filename = card.querySelector('[data-filename]')?.getAttribute('data-filename') ?? ''
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

    const bulk = within(tree).getByRole('treeitem', { name: 'Bulk' })
    expect(bulk).toHaveAccessibleDescription('300 files')
    await user.click(bulk)
    expect(bulk).toHaveAttribute('aria-selected', 'true')
    expect(within(tree).getByRole('treeitem', { name: 'Top level' })).toHaveAttribute('aria-selected', 'false')
    expect(within(tree).getByRole('treeitem', { name: 'MakerWorld' })).toHaveAccessibleDescription('2 files')
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
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete drawer-label.3mf?' })
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
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete 3 files?' })
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
      expect(within(screen.getByTestId('library-file-67')).getByTestId('library-name-67')).toHaveAttribute(
        'data-filename',
        'Wand for Clara.3mf',
      ),
    )
  })
})

/** #3650 — a folder's row in the sidebar tree: what a right-click lands on. */
function folderRow(id: number): HTMLElement {
  return screen.getByTestId(`library-folder-${id}`).querySelector<HTMLElement>('[data-row]')!
}

describe('LibraryPage, folders and projects (#3650)', () => {
  beforeEach(() => {
    resetMockState()
    window.localStorage.clear()
  })

  it('opens a folder’s menu on right-click, and from the keyboard on its row', async () => {
    renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')

    fireEvent.contextMenu(folderRow(13))
    const menu = await screen.findByRole('menu', { name: 'Lamp' })
    expect(within(menu).getByRole('menuitem', { name: 'Rename…' })).toBeInTheDocument()
    expect(within(menu).getByRole('menuitem', { name: 'Open the project Desk lamp' })).toBeInTheDocument()
    fireEvent.keyDown(menu, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())

    // Shift+F10 on the focused treeitem opens the same menu.
    const item = screen.getByTestId('library-folder-1')
    item.focus()
    fireEvent.keyDown(item, { key: 'F10', shiftKey: true })
    expect(await screen.findByRole('menu', { name: 'MakerWorld' })).toBeInTheDocument()
  })

  it('renames the open folder from its menu, and the URL follows it', async () => {
    const posted: unknown[] = []
    const listen = async ({ request }: { request: Request }) => {
      if (request.method === 'PATCH') posted.push(await request.clone().json())
    }
    server.events.on('request:start', listen)
    onTestFinished(() => server.events.removeListener('request:start', listen))
    const { user } = renderPage(<><LibraryPage /><Where /></>, { route: '/library/Spec/MakerWorld/Work' })
    await screen.findByTestId('library-file-120')

    await user.click(screen.getByTestId('library-folder-actions'))
    await user.click(await screen.findByRole('menuitem', { name: 'Rename…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Rename the folder Work' })
    const field = within(dialog).getByRole('textbox', { name: 'Folder name' })
    await user.clear(field)
    await user.type(field, 'Desk{Enter}')

    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(/^\/library\/Spec\/MakerWorld\/Desk$/))
    expect(posted).toEqual([{ name: 'Desk' }])
    expect(await screen.findByTestId('library-file-120')).toBeInTheDocument()
  })

  it('moves a folder, never into itself, and deletes one after a confirmation', async () => {
    const { user } = renderPage(<><LibraryPage /><Where /></>, { route: '/library/Spec' })
    await screen.findByTestId('library-subfolder-11')

    fireEvent.contextMenu(folderRow(10))
    await user.click(await screen.findByRole('menuitem', { name: 'Move to…' }))
    const move = await screen.findByRole('dialog', { name: 'Move the folder Spec to…' })
    const tree = within(move).getByRole('tree', { name: 'Destination folder' })
    // Spec and the folders inside it are not offered.
    expect(within(tree).queryByRole('treeitem', { name: 'Spec' })).toBeNull()
    expect(within(move).getByTestId('library-folder-move-confirm')).toBeDisabled()
    await user.click(within(tree).getByRole('treeitem', { name: 'Supplies' }))
    await user.click(within(move).getByTestId('library-folder-move-confirm'))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(/^\/library\/Supplies\/Spec$/))

    await user.click(screen.getByTestId('library-folder-actions'))
    await user.click(await screen.findByRole('menuitem', { name: 'Delete…' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Delete the folder Spec?' })
    expect(confirm).toHaveTextContent('Its 3 files go to Bambuddy')
    await user.click(within(confirm).getByTestId('library-folder-delete-confirm'))

    expect(await screen.findByTestId('library-deleted')).toHaveTextContent(
      "Deleted the folder Spec. Moved 2 files to Bambuddy's trash. Removed 1 external file for good.",
    )
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(/^\/library\/Supplies$/))
  })

  it('makes a folder inside the open one', async () => {
    const { user } = renderPage(<><LibraryPage /><Where /></>, { route: '/library/MakerWorld' })
    await screen.findByTestId('library-file-67')
    await user.click(screen.getByTestId('library-new-folder'))
    const dialog = await screen.findByRole('dialog', { name: 'New folder' })
    expect(dialog).toHaveTextContent('In MakerWorld.')
    await user.type(within(dialog).getByRole('textbox', { name: 'Folder name' }), 'Wands{Enter}')
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(/^\/library\/MakerWorld\/Wands$/))
  })

  it('counts a folder name in characters, as the backend does, up to 255 (#3733 review)', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library/MakerWorld' })
    await screen.findByTestId('library-file-67')
    await user.click(screen.getByTestId('library-new-folder'))
    const dialog = await screen.findByRole('dialog', { name: 'New folder' })
    const name = within(dialog).getByRole('textbox', { name: 'Folder name' })
    const confirm = within(dialog).getByTestId('library-folder-name-confirm')

    // An emoji is two UTF-16 units but one character: 255 of them are a usable name.
    fireEvent.change(name, { target: { value: '🪄'.repeat(255) } })
    expect(confirm).toBeEnabled()
    fireEvent.change(name, { target: { value: '🪄'.repeat(256) } })
    expect(confirm).toBeDisabled()
    fireEvent.change(name, { target: { value: 'a/b' } })
    expect(confirm).toBeDisabled()
  })

  it('arranges a folder’s files from its menu', async () => {
    const { user } = renderPage(<LibraryPage />, { route: '/library' })
    await screen.findByTestId('library-file-89')
    fireEvent.contextMenu(folderRow(1))
    await user.click(await screen.findByRole('menuitem', { name: 'Arrange its files…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Arrange' })
    expect(dialog).toHaveTextContent("Clara's Wand")
  })

  it('browses by project, each view linking to the other', async () => {
    const { user } = renderPage(<><LibraryPage /><Where /></>, { route: '/library/Lamp' })
    await screen.findByTestId('library-file-130')

    // The folder names its project, as a link to the project view.
    await user.click(screen.getByTestId('library-folder-project-link'))
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/library\?project=7$/)
    expect(screen.getByRole('radio', { name: 'Projects' })).toHaveAttribute('aria-checked', 'true')
    const projects = screen.getByRole('tree', { name: 'Library projects' })
    expect(within(projects).getByRole('treeitem', { name: 'Desk lamp' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByTestId('library-file-130')).toBeInTheDocument()

    // The project names its folder, as a link back to the folder view.
    await user.click(screen.getByTestId('library-project-folder-13'))
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/library\/Lamp$/)

    // The toggle lists every project, with no files until one is chosen.
    await user.click(screen.getByRole('radio', { name: 'Projects' }))
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/library\?project=7$/)
    await user.click(within(screen.getByRole('tree', { name: 'Library projects' })).getByRole('treeitem', { name: 'All projects' }))
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/library\?by=project$/)
    expect(await screen.findByTestId('library-project-card-7')).toHaveTextContent('Desk lamp')
    await user.click(screen.getByRole('radio', { name: 'Folders' }))
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/library$/)
  })

  it('says which folder a project of several shows (#3719 review)', async () => {
    libraryFolders.push({ id: 14, name: 'Lamp parts', parent_id: null, depth: 0, file_count: 0, project_id: 7, project_name: 'Desk lamp' })
    onTestFinished(() => void libraryFolders.pop())
    renderPage(<LibraryPage />, { route: '/library?project=7' })
    expect(await screen.findByTestId('library-file-130')).toBeInTheDocument()
    expect(screen.getByTestId('library-project-folder-14')).toBeInTheDocument()
    expect(screen.getByTestId('library-project-shown')).toHaveTextContent('Showing the files in Lamp.')
  })
})

function Where() {
  const location = useLocation()
  return <p data-testid="where">{`${location.pathname}${location.search}`}</p>
}
