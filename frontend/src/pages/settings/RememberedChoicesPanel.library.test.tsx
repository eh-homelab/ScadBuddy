import { fireEvent, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, onTestFinished } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { LIBRARY_NAMES_PER_REQUEST, RememberedChoicesPanel } from './RememberedChoicesPanel'

/** Library file 89's name in the mock library (mocks/features/library.ts). */
const NAME_89 = 'bag-clip-3155628dc2bb43d4940fad6dba164efc.3mf'

describe('RememberedChoicesPanel · library files (#1754)', () => {
  it("names a library file's entries and forgets each through its own route", async () => {
    const choices: unknown[] = []
    const options: unknown[] = []
    server.use(
      http.get('/api/v1/settings/remembered', () =>
        HttpResponse.json({
          model_print_choices: { 'library:89': { printer_id: null, tier: 'fine' } },
          model_print_options: { 'library:89': { timelapse: true } },
        }),
      ),
      http.put('/api/v1/print/library/89/choices', async ({ request }) => {
        choices.push(await request.json())
        return HttpResponse.json({})
      }),
      http.put('/api/v1/settings/print-options', async ({ request }) => {
        options.push(await request.json())
        return HttpResponse.json({ defaults: {}, global_options: {}, printers: {}, models: {} })
      }),
    )
    renderPage(<RememberedChoicesPanel targets={null} />)

    fireEvent.click(await screen.findByRole('button', { name: `Forget printer and spools for ${NAME_89}` }))
    await waitFor(() => expect(choices).toEqual([{}]))
    fireEvent.click(screen.getByRole('button', { name: `Forget print options for ${NAME_89}` }))
    await waitFor(() => expect(options).toEqual([{ scope: 'model', key: 'library:89', options: {} }]))
  })

  // #2349: the row says which file it is, and a file Bambuddy no longer has says so.
  it('names each file, links it to the Library, and marks one Bambuddy no longer has', async () => {
    const asked: string[] = []
    server.use(
      http.get('/api/v1/settings/remembered', () =>
        HttpResponse.json({
          model_print_choices: { 'library:89': { printer_id: null, tier: 'fine' }, 'library:9999': { tier: 'draft' } },
          model_print_options: { 'library:89': { timelapse: true } },
        }),
      ),
    )
    const record = ({ request }: { request: Request }) => {
      if (request.url.includes('/print/library/names')) asked.push(new URL(request.url).search)
    }
    server.events.on('request:start', record)
    onTestFinished(() => server.events.removeListener('request:start', record))
    renderPage(<RememberedChoicesPanel targets={null} />)

    const links = await screen.findAllByRole('link', { name: NAME_89 })
    expect(links.map((link) => link.getAttribute('href'))).toEqual(['/library?file=89', '/library?file=89'])
    expect(screen.getByRole('button', { name: 'Forget printer and spools for deleted file 9999' })).toBeInTheDocument()
    expect(screen.queryByText(/^Library file /)).toBeNull()
    // One lookup for every row, each file once.
    expect(asked).toEqual(['?ids=89&ids=9999'])
  })

  it("falls back to the file's id when the names cannot be read", async () => {
    server.use(
      http.get('/api/v1/settings/remembered', () =>
        HttpResponse.json({ model_print_choices: { 'library:89': { tier: 'fine' } } }),
      ),
      http.get('/api/v1/print/library/names', () => HttpResponse.json({ detail: 'Bambuddy is down' }, { status: 502 })),
    )
    renderPage(<RememberedChoicesPanel targets={null} />)
    expect(await screen.findByRole('button', { name: 'Forget printer and spools for Library file 89' })).toBeInTheDocument()
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('asks for the names in requests the route takes, however many files there are', async () => {
    const count = LIBRARY_NAMES_PER_REQUEST + 20
    const FIRST = 5000
    const asked: number[][] = []
    server.use(
      http.get('/api/v1/settings/remembered', () =>
        HttpResponse.json({
          model_print_choices: Object.fromEntries(
            Array.from({ length: count }, (_, n) => [`library:${FIRST + n}`, { tier: 'fine' }]),
          ),
        }),
      ),
      http.get('/api/v1/print/library/names', ({ request }) => {
        const ids = new URL(request.url).searchParams.getAll('ids').map(Number)
        // Only this test's files: an earlier test's lookup can still land here.
        if (ids.every((id) => id >= FIRST)) asked.push(ids)
        if (ids.length > LIBRARY_NAMES_PER_REQUEST) return HttpResponse.json({ detail: 'too many' }, { status: 422 })
        return HttpResponse.json(ids.map((id) => ({ id, name: `file-${id}.3mf`, folder_id: null })))
      }),
    )
    renderPage(<RememberedChoicesPanel targets={null} />)
    expect(await screen.findByRole('link', { name: `file-${FIRST + count - 1}.3mf` })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: `file-${FIRST}.3mf` })).toBeInTheDocument()
    // Sent together, so in either order.
    expect(asked.map((ids) => ids.length).sort((a, b) => a - b)).toEqual([20, LIBRARY_NAMES_PER_REQUEST])
  })

  it('keeps the names on the other rows while a Forget asks again', async () => {
    let remembered: Record<string, unknown> = { 'library:89': { tier: 'fine' }, 'library:9999': { tier: 'draft' } }
    let answered = 0
    let release: () => void = () => {}
    server.use(
      http.get('/api/v1/settings/remembered', () => HttpResponse.json({ model_print_choices: remembered })),
      http.put('/api/v1/print/library/9999/choices', () => {
        remembered = { 'library:89': { tier: 'fine' } }
        return HttpResponse.json({})
      }),
      http.get('/api/v1/print/library/names', async ({ request }) => {
        const ids = new URL(request.url).searchParams.getAll('ids').map(Number)
        // The second ask, after the Forget, waits until the test lets it answer.
        if (answered++ > 0) await new Promise<void>((resolve) => (release = resolve))
        return HttpResponse.json(ids.map((id) => ({ id, name: id === 89 ? NAME_89 : null, folder_id: null })))
      }),
    )
    renderPage(<RememberedChoicesPanel targets={null} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Forget printer and spools for deleted file 9999' }))
    await waitFor(() => expect(screen.queryByText(/deleted file 9999/)).toBeNull())
    await waitFor(() => expect(answered).toBe(2))
    // Asked again and not yet answered: still the name, never "Library file 89".
    expect(screen.getByRole('link', { name: NAME_89 })).toBeInTheDocument()
    expect(screen.queryByText(/^Library file /)).toBeNull()
    release()
    expect(await screen.findByRole('link', { name: NAME_89 })).toBeInTheDocument()
  })
})
