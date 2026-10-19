import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { BUILTIN_SLUG } from '../../mocks/fixtures'
import {
  BREAKING_MESSAGE,
  BREAKING_REF,
  SLOW_MESSAGE,
  SLOW_REF,
  UNCHECKED_REF,
  setMockLibraryPin,
  setMockUnusedCheckouts,
} from '../../mocks/features/libraryUpgrade'
import { printRunPoll } from '../../api/client'
import { mockModels, problem, replaceMockModel, setMockInvalidLibraries } from '../../mocks/handlers'
import { emitRealtime } from '../../mocks/realtime'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { LibraryUpgrade } from './LibraryUpgrade'
import { choose, optionsOf } from '../../test/select'

const URL_ = 'https://github.com/BelfrySCAD/BOSL2.git'
const OLD = { name: 'BOSL2', url: URL_, ref: 'v2.0.700', commit: 'a1b2c3d4'.repeat(5) }

/** Every request the flow sends to a library route, in order. */
function recorded() {
  const calls: { method: string; path: string; body: unknown }[] = []
  const record = ({ request }: { request: Request }) => {
    const path = new URL(request.url).pathname
    if (request.method === 'GET' || !path.includes('/libraries/')) return
    void request
      .clone()
      .json()
      .then((body: unknown) => calls.push({ method: request.method, path, body }))
  }
  server.events.on('request:start', record)
  // Left on, it reads every later test's library writes too, a body-less DELETE included.
  onTestFinished(() => server.events.removeListener('request:start', record))
  return calls
}

async function open(ref?: string) {
  const view = renderPage(<LibraryUpgrade />)
  const { user } = view
  await choose(user, await screen.findByLabelText('Library'), 'BOSL2')
  const input = screen.getByLabelText('Candidate ref')
  if (ref !== undefined) {
    await user.clear(input)
    await user.type(input, ref)
  }
  await screen.findByRole('list', { name: 'Models that pin BOSL2' })
  // Rows are named by the model once the model list has answered.
  await screen.findByRole('listitem', { name: 'Name Keychain' })
  return view
}

const row = (name: string) => screen.getByRole('listitem', { name })

describe('LibraryUpgrade', () => {
  beforeEach(() => {
    setMockLibraryPin('name-keychain', OLD)
    setMockLibraryPin('gridfinity-bin', { ...OLD, ref: 'v2.0.600', commit: 'e5f6a7b8'.repeat(5) })
  })

  it('lists the models that pin the library with their ref and commit, and an invalid entry as unmovable', async () => {
    setMockInvalidLibraries('creme-coaster', [{ index: 0, name: 'BOSL2', problem: 'no commit' }])
    await open()
    expect(screen.getByLabelText('Candidate ref')).toHaveValue('v2.0.761')

    const keychain = row('Name Keychain')
    expect(keychain).toHaveTextContent('Pinned to v2.0.700 at a1b2c3d')
    expect(within(keychain).getByRole('checkbox', { name: 'Move Name Keychain' })).not.toBeChecked()
    expect(row('Gridfinity Bin')).toHaveTextContent('Pinned to v2.0.600 at e5f6a7b')

    const invalid = row('Crème Coaster')
    expect(invalid).toHaveTextContent(/Invalid/)
    expect(within(invalid).queryByRole('checkbox')).toBeNull()
    expect(within(invalid).queryByRole('button', { name: 'Check' })).toBeNull()
  })

  it('offers an installed library the catalogue does not list, and lists the models that pin it', async () => {
    const pin = { name: 'threads', url: 'https://github.com/rcolyer/threads-scad.git', ref: 'v2.1', commit: 'c0ffee00'.repeat(5) }
    setMockLibraryPin('gridfinity-bin', pin)
    const { user } = renderPage(<LibraryUpgrade />)
    const select = await screen.findByLabelText('Library')
    expect(await optionsOf(user, select)).toContain('threads')
    await choose(user, select, 'threads')
    // Not in the catalogue, so no ref is suggested.
    expect(screen.getByLabelText('Candidate ref')).toHaveValue('')
    const list = await screen.findByRole('list', { name: 'Models that pin threads' })
    const gridfinity = await within(list).findByRole('listitem', { name: 'Gridfinity Bin' })
    expect(gridfinity).toHaveTextContent('Pinned to v2.1 at c0ffee0')
    expect(within(list).getAllByRole('listitem')).toHaveLength(1)
  })

  it('offers a library pinned elsewhere while the panel is open (#766)', async () => {
    const { user } = renderPage(<LibraryUpgrade />)
    const select = await screen.findByLabelText('Library')
    expect(await optionsOf(user, select)).not.toContain('threads')

    setMockLibraryPin('gridfinity-bin', { name: 'threads', url: 'https://github.com/rcolyer/threads-scad.git', ref: 'v2.1', commit: 'c0ffee00'.repeat(5) })
    // Re-sent until it lands: an event before the subscription is confirmed reaches no one.
    await waitFor(async () => {
      emitRealtime('library.changed', ['libraries', 'model:gridfinity-bin'], { slug: 'gridfinity-bin', name: 'threads' })
      expect(await optionsOf(user, screen.getByLabelText('Library'))).toContain('threads')
    })
  })

  it('reads the models once, not again on every switch of library', async () => {
    setMockLibraryPin('name-keychain', { name: 'threads', url: 'https://github.com/rcolyer/threads-scad.git', ref: 'v2.1', commit: 'c0ffee00'.repeat(5) })
    let reads = 0
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'GET' && new URL(request.url).pathname === '/api/v1/models') reads += 1
    })
    const { user } = await open()
    // The mount's read, and the live-update resync's: neither depends on the library.
    const mounted = reads
    expect(mounted).toBeGreaterThan(0)
    await choose(user, screen.getByLabelText('Library'), 'threads')
    // Named at once from the list already read: no slug while a fetch is in flight.
    const list = await screen.findByRole('list', { name: 'Models that pin threads' })
    expect(within(list).getByRole('listitem', { name: 'Name Keychain' })).toBeInTheDocument()
    await choose(user, screen.getByLabelText('Library'), 'BOSL2')
    await screen.findByRole('list', { name: 'Models that pin BOSL2' })
    expect(row('Name Keychain')).toBeInTheDocument()
    expect(reads).toBe(mounted)
  })

  it('says so when the models cannot be read, and still lists the pins by slug', async () => {
    server.use(http.get('/api/v1/models', () => problem(503, 'Service Unavailable', 'the catalogue is offline')))
    const { user } = renderPage(<LibraryUpgrade />)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not read the models, so rows show slugs: the catalogue is offline',
    )
    await choose(user, screen.getByLabelText('Library'), 'BOSL2')
    expect(await screen.findByRole('listitem', { name: 'name-keychain' })).toHaveTextContent('Pinned to v2.0.700')
  })

  it('says so when no model pins the library', async () => {
    const { user } = renderPage(<LibraryUpgrade />)
    await choose(user, await screen.findByLabelText('Library'), 'dotSCAD')
    expect(await screen.findByText('No model pins dotSCAD.')).toBeInTheDocument()
  })

  it('checks one model against the candidate on request, one check at a time', async () => {
    const calls = recorded()
    const { user } = await open()
    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    // While it runs, no other check (and no move) can start.
    expect(within(row('Gridfinity Bin')).getByRole('button', { name: 'Check' })).toBeDisabled()

    const result = await within(row('Name Keychain')).findByTestId('library-check')
    expect(result).toHaveTextContent(/^Parses at v2\.0\.761 \([0-9a-f]{7}\) — 4 parameters\.$/)
    expect(within(row('Gridfinity Bin')).queryByTestId('library-check')).toBeNull()
    expect(within(row('Gridfinity Bin')).getByRole('button', { name: 'Check' })).toBeEnabled()
    expect(calls).toEqual([
      { method: 'POST', path: '/api/v1/models/name-keychain/libraries/BOSL2/check', body: { ref: 'v2.0.761' } },
    ])
  })

  it('marks a check stale once the candidate ref changes, and current again when it changes back', async () => {
    const { user } = await open()
    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    await within(row('Name Keychain')).findByTestId('library-check')

    const input = screen.getByLabelText('Candidate ref')
    await user.clear(input)
    await user.type(input, 'v2.0.800')
    expect(within(row('Name Keychain')).queryByTestId('library-check')).toBeNull()
    expect(within(row('Name Keychain')).queryByText(/Parses/)).toBeNull()
    expect(within(row('Name Keychain')).getByTestId('library-check-stale')).toHaveTextContent(
      'Checked at v2.0.761, not v2.0.800; check again.',
    )

    await user.clear(input)
    await user.type(input, 'v2.0.761')
    expect(within(row('Name Keychain')).getByTestId('library-check')).toHaveTextContent('Parses at v2.0.761')
    expect(within(row('Name Keychain')).queryByTestId('library-check-stale')).toBeNull()
  })

  it('lets rows be ticked while a check runs, but not during a move', async () => {
    server.use(
      http.post('/api/v1/models/:slug/libraries/:name/check', async () => {
        await delay(300)
        return undefined
      }),
      http.patch('/api/v1/models/:slug/libraries/:name', async () => {
        await delay(300)
        return undefined
      }),
    )
    const { user } = await open()
    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    const gridfinity = screen.getByRole('checkbox', { name: 'Move Gridfinity Bin' })
    expect(gridfinity).toBeEnabled()
    await user.click(gridfinity)
    expect(gridfinity).toBeChecked()
    // Moving waits for the check: both hold the server's checkout gate.
    expect(screen.getByRole('button', { name: 'Move 1 model to v2.0.761' })).toBeDisabled()
    await within(row('Name Keychain')).findByTestId('library-check')

    await user.click(screen.getByRole('button', { name: 'Move 1 model to v2.0.761' }))
    expect(screen.getByRole('checkbox', { name: 'Move Name Keychain' })).toBeDisabled()
    expect(await within(row('Gridfinity Bin')).findByTestId('library-moved')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Move Name Keychain' })).toBeEnabled()
  })

  it('aborts an in-flight check when the rows unmount, so it stops holding the check permit', async () => {
    let started = false
    let received: AbortSignal | undefined
    server.use(
      http.post('/api/v1/models/:slug/libraries/:name/check', async ({ request }) => {
        started = true
        received = request.signal
        await delay('infinite')
        return undefined
      }),
    )
    // The signal given to fetch, and the one the handler's request carries: msw rebuilds
    // that request from the wire, so its abort says the abort reached the wire too
    // (@mswjs/interceptors 0.45.8, #2855).
    const sent = vi.spyOn(globalThis, 'fetch')
    onTestFinished(() => sent.mockRestore())
    const { user, unmount } = await open()
    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    await waitFor(() => expect(started).toBe(true))
    const checks = sent.mock.calls.filter(([url]) => /\/libraries\/[^/]+\/check$/.test(String(url)))
    expect(checks).toHaveLength(1)
    const inflight = checks[0]?.[1]?.signal
    unmount()
    await waitFor(() => expect(inflight?.aborted).toBe(true))
    await waitFor(() => expect(received?.aborted).toBe(true))
  })

  it('disables the Library select while a check or a move runs', async () => {
    server.use(
      http.post('/api/v1/models/:slug/libraries/:name/check', async () => {
        await delay(300)
        return undefined
      }),
      http.patch('/api/v1/models/:slug/libraries/:name', async () => {
        await delay(300)
        return undefined
      }),
    )
    const { user } = await open()
    const select = screen.getByLabelText('Library')
    expect(select).toBeEnabled()

    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    expect(select).toBeDisabled()
    await within(row('Name Keychain')).findByTestId('library-check')
    await waitFor(() => expect(select).toBeEnabled())

    await user.click(screen.getByRole('checkbox', { name: 'Move Name Keychain' }))
    await user.click(screen.getByRole('button', { name: 'Move 1 model to v2.0.761' }))
    expect(select).toBeDisabled()
    await within(row('Name Keychain')).findByTestId('library-moved')
    await waitFor(() => expect(select).toBeEnabled())
  })

  it('holds the candidate ref while a move runs, but not while a check runs (#771)', async () => {
    let release: () => void = () => {}
    server.use(
      http.post('/api/v1/models/:slug/libraries/:name/check', async () => {
        await delay(300)
        return undefined
      }),
      http.patch('/api/v1/models/:slug/libraries/:name', async () => {
        await new Promise<void>((resolve) => {
          release = resolve
        })
        return undefined
      }),
    )
    const { user } = await open()
    const input = screen.getByLabelText('Candidate ref')

    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    expect(input).toBeEnabled()
    await within(row('Name Keychain')).findByTestId('library-check')

    await user.click(screen.getByRole('checkbox', { name: 'Move Name Keychain' }))
    await user.click(screen.getByRole('button', { name: 'Move 1 model to v2.0.761' }))
    await waitFor(() => expect(input).toBeDisabled())
    // Typing into it changes nothing: the button keeps naming the ref the move uses.
    await user.type(input, 'x')
    expect(input).toHaveValue('v2.0.761')
    expect(screen.getByRole('button', { name: 'Move 1 model to v2.0.761' })).toBeInTheDocument()

    release()
    await within(row('Name Keychain')).findByTestId('library-moved')
    await waitFor(() => expect(input).toBeEnabled())
  })

  it('shows a failing check with its diagnostics and log tail', async () => {
    const { user } = await open(BREAKING_REF)
    await user.click(within(row('Gridfinity Bin')).getByRole('button', { name: 'Check' }))
    const result = await within(row('Gridfinity Bin')).findByTestId('library-check')
    expect(result).toHaveTextContent(`Does not parse at ${BREAKING_REF}`)
    expect(within(result).getByRole('list', { name: 'Diagnostics' })).toHaveTextContent(
      `model.scad:1 ${BREAKING_MESSAGE}`,
    )
    expect(within(result).getByText(/Execution aborted/)).toBeInTheDocument()
  })

  it('shows a check killed on the render timeout as timed out, not as a parse failure', async () => {
    const { user } = await open(SLOW_REF)
    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    const result = await within(row('Name Keychain')).findByTestId('library-check')
    expect(result).toHaveTextContent(new RegExp(`^Timed out at ${SLOW_REF.replace(/\./g, '\\.')} \\([0-9a-f]{7}\\)\\.`))
    expect(result).not.toHaveTextContent('Does not parse')
    expect(within(result).getByRole('list', { name: 'Diagnostics' })).toHaveTextContent(SLOW_MESSAGE)
    expect(within(result).queryByText('OpenSCAD log')).toBeNull()
  })

  it('shows a check no OpenSCAD could run as not checked, not as a pass', async () => {
    const { user } = await open(UNCHECKED_REF)
    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    const result = await within(row('Name Keychain')).findByTestId('library-check')
    expect(result).toHaveTextContent(
      new RegExp(`^Not checked at ${UNCHECKED_REF.replace(/\./g, '\\.')} \\([0-9a-f]{7}\\): no OpenSCAD was available to ask\\.$`),
    )
    expect(result).not.toHaveTextContent('Parses')
    expect(within(result).queryByRole('list', { name: 'Diagnostics' })).toBeNull()
  })

  it('shows a refused check on its row', async () => {
    const { user } = await open('v9.9.9')
    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    expect(await within(row('Name Keychain')).findByRole('alert')).toHaveTextContent(
      'Check failed: git clone failed',
    )
  })

  it('re-pins only the ticked models, one request each', async () => {
    const calls = recorded()
    const { user } = await open('v2.0.761')
    await user.click(screen.getByRole('checkbox', { name: 'Move Name Keychain' }))
    await user.click(screen.getByRole('button', { name: 'Move 1 model to v2.0.761' }))

    expect(await within(row('Name Keychain')).findByTestId('library-moved')).toHaveTextContent(/^Moved to v2\.0\.761 at [0-9a-f]{7}$/)
    expect(row('Name Keychain')).toHaveTextContent('Pinned to v2.0.761')
    expect(row('Gridfinity Bin')).toHaveTextContent('Pinned to v2.0.600 at e5f6a7b')
    expect(calls).toEqual([
      { method: 'PATCH', path: '/api/v1/models/name-keychain/libraries/BOSL2', body: { ref: 'v2.0.761' } },
    ])
    expect(screen.getByRole('checkbox', { name: 'Move Name Keychain' })).not.toBeChecked()
  })

  it('keeps the other rows when one re-pin is a 409', async () => {
    const order: string[] = []
    server.use(
      http.patch('/api/v1/models/:slug/libraries/:name', async ({ params }) => {
        order.push(String(params['slug']))
        if (params['slug'] === 'name-keychain') {
          await delay(20)
          return problem(409, 'Conflict', "'name-keychain's 'BOSL2' was changed or removed while this re-pin ran")
        }
        return undefined
      }),
    )
    const { user } = await open('v2.0.761')
    await user.click(screen.getByRole('checkbox', { name: 'Move Name Keychain' }))
    await user.click(screen.getByRole('checkbox', { name: 'Move Gridfinity Bin' }))
    await user.click(screen.getByRole('button', { name: 'Move 2 models to v2.0.761' }))

    expect(await within(row('Gridfinity Bin')).findByTestId('library-moved')).toHaveTextContent(/^Moved to v2\.0\.761 at [0-9a-f]{7}$/)
    expect(within(row('Name Keychain')).getByRole('alert')).toHaveTextContent(
      'Not moved: \'name-keychain\'s \'BOSL2\' was changed or removed',
    )
    expect(row('Name Keychain')).toHaveTextContent('Pinned to v2.0.700 at a1b2c3d')
    // The refused one stays ticked, to try again; the moved one does not.
    expect(screen.getByRole('checkbox', { name: 'Move Name Keychain' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Move Gridfinity Bin' })).not.toBeChecked()
    expect(order).toEqual(['name-keychain', 'gridfinity-bin'])
  })

  it('treats a re-pin whose answer lacks the pin as not moved, and keeps the row ticked', async () => {
    server.use(
      http.patch('/api/v1/models/:slug/libraries/:name', () =>
        HttpResponse.json(mockModels().find((model) => model.slug === 'name-keychain')!, { status: 200 }),
      ),
    )
    const { user } = await open('v2.0.761')
    const selected = mockModels().find((model) => model.slug === 'name-keychain')!
    replaceMockModel({ ...selected, libraries: [] })
    await user.click(screen.getByRole('checkbox', { name: 'Move Name Keychain' }))
    await user.click(screen.getByRole('button', { name: 'Move 1 model to v2.0.761' }))

    expect(await within(row('Name Keychain')).findByRole('alert')).toHaveTextContent(
      /^Not moved: the re-pin answered, but the model it returned does not pin BOSL2/,
    )
    expect(within(row('Name Keychain')).queryByTestId('library-moved')).toBeNull()
    expect(screen.getByRole('checkbox', { name: 'Move Name Keychain' })).toBeChecked()
  })

  it('offers a built-in a check but no re-pin', async () => {
    setMockLibraryPin(BUILTIN_SLUG, OLD)
    const { user } = await open()
    const builtin = row('Keychain Template')
    expect(builtin).toHaveTextContent('Built-in: it can be checked, but not re-pinned.')
    expect(within(builtin).queryByRole('checkbox')).toBeNull()
    await user.click(within(builtin).getByRole('button', { name: 'Check' }))
    expect(await within(builtin).findByTestId('library-check')).toHaveTextContent('Parses at v2.0.761')
  })

  // #2770: a checkout no live pin reads can be removed; a pinned one is not offered.
  it('lists the checkouts no model uses, and removes one by its commit', async () => {
    const stale = { name: 'BOSL2', commit: 'deadbeef'.repeat(5), used_by: [] }
    setMockUnusedCheckouts([stale])
    const sent: string[] = []
    const record = ({ request }: { request: Request }) => {
      if (request.method === 'DELETE') sent.push(new URL(request.url).pathname + new URL(request.url).search)
    }
    server.events.on('request:start', record)
    onTestFinished(() => server.events.removeListener('request:start', record))
    const { user } = renderPage(<LibraryUpgrade />)
    const list = await screen.findByRole('list', { name: 'Checkouts no model uses' })
    expect(within(list).getAllByRole('listitem').map((item) => item.getAttribute('aria-label'))).toEqual(['BOSL2 deadbee'])
    await user.click(within(list).getByRole('button', { name: 'Remove BOSL2 deadbee' }))
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Checkouts no model uses' })).toBeNull())
    expect(sent).toEqual([`/api/v1/libraries/BOSL2?commit=${stale.commit}`])
  })

  it('follows a removal answered 202 through its operation to the end', async () => {
    const poll = printRunPoll.intervalMs
    printRunPoll.intervalMs = 1
    onTestFinished(() => {
      printRunPoll.intervalMs = poll
    })
    setMockUnusedCheckouts([{ name: 'BOSL2', commit: 'deadbeef'.repeat(5), used_by: [] }])
    const op = { id: 'op-remove', kind: 'library_remove', status: 'running', created_at: '2026-10-14T00:00:00Z' }
    let reads = 0
    server.use(
      http.delete('/api/v1/libraries/:name', () => HttpResponse.json(op, { status: 202 })),
      http.get('/api/v1/operations/op-remove', () => {
        reads += 1
        if (reads < 2) return HttpResponse.json(op)
        setMockUnusedCheckouts([])
        return HttpResponse.json({ ...op, status: 'succeeded', result: null })
      }),
    )
    const { user } = renderPage(<LibraryUpgrade />)
    const list = await screen.findByRole('list', { name: 'Checkouts no model uses' })
    await user.click(within(list).getByRole('button', { name: 'Remove BOSL2 deadbee' }))
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Checkouts no model uses' })).toBeNull())
    expect(reads).toBe(2)
  })

  // A refusal shown beside another row is not kept next to a row that may now be removable.
  it("drops another row's refusal once a removal goes through", async () => {
    const [a, b] = ['deadbeef', 'feedface'].map((c) => ({ name: 'BOSL2', commit: c.repeat(5), used_by: [] }))
    setMockUnusedCheckouts([a!, b!])
    server.use(
      http.delete('/api/v1/libraries/:name', ({ request }) => {
        if (new URL(request.url).searchParams.get('commit') === a!.commit) {
          return problem(409, 'Conflict', 'a running render reads BOSL2 deadbeef: job j1')
        }
        setMockUnusedCheckouts([a!])
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = renderPage(<LibraryUpgrade />)
    const list = await screen.findByRole('list', { name: 'Checkouts no model uses' })
    await user.click(within(list).getByRole('button', { name: 'Remove BOSL2 deadbee' }))
    expect(await within(list).findByRole('alert')).toHaveTextContent('a running render reads BOSL2 deadbeef')
    await user.click(within(list).getByRole('button', { name: 'Remove BOSL2 feedfac' }))
    await waitFor(() => {
      const left = screen.getByRole('list', { name: 'Checkouts no model uses' })
      expect(within(left).getAllByRole('listitem').map((item) => item.getAttribute('aria-label'))).toEqual([
        'BOSL2 deadbee',
      ])
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps the chosen library, its checks and its ticks on screen while a removal re-lists the checkouts', async () => {
    setMockUnusedCheckouts([{ name: 'BOSL2', commit: 'deadbeef'.repeat(5), used_by: [] }])
    const { user } = await open()
    // A checked, ticked row: the state the old reload threw away.
    await user.click(within(row('Name Keychain')).getByRole('button', { name: 'Check' }))
    await within(row('Name Keychain')).findByTestId('library-check')
    await user.click(screen.getByRole('checkbox', { name: 'Move Name Keychain' }))
    const list = screen.getByRole('list', { name: 'Checkouts no model uses' })
    await user.click(within(list).getByRole('button', { name: 'Remove BOSL2 deadbee' }))
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Checkouts no model uses' })).toBeNull())
    // No "Loading libraries" in between: the library, its checks and ticks stay.
    expect(screen.queryByText('Loading libraries')).toBeNull()
    expect(screen.getByLabelText('Library')).toHaveTextContent('BOSL2')
    expect(within(row('Name Keychain')).getByTestId('library-check')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Move Name Keychain' })).toBeChecked()
  })

  /** Re-lists that fail while `down` is set; removing `name` sets it, as the store went down. */
  function storeDownOnRemoval(name: string) {
    const store = { down: false }
    server.use(
      http.delete('/api/v1/libraries/:name', ({ params }) => {
        if (params['name'] === name) store.down = true
        return undefined
      }),
      http.get('/api/v1/libraries/installed', () =>
        store.down ? problem(503, 'Unavailable', 'the store is down') : undefined,
      ),
    )
    return store
  }

  it('says so when a removal could not re-list the checkouts (#3548), until a read lands (#3839)', async () => {
    setMockUnusedCheckouts([{ name: 'BOSL2', commit: 'deadbeef'.repeat(5), used_by: [] }])
    const store = storeDownOnRemoval('BOSL2')
    const { user } = renderPage(<LibraryUpgrade />)
    const list = await screen.findByRole('list', { name: 'Checkouts no model uses' })
    await user.click(within(list).getByRole('button', { name: 'Remove BOSL2 deadbee' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not re-list the checkouts')
    // The store is back and a realtime refresh lists the checkouts: the alert goes with it.
    store.down = false
    await waitFor(() => {
      emitRealtime('library.changed', ['libraries'], { name: 'BOSL2' })
      expect(screen.queryByRole('alert')).toBeNull()
    })
    expect(screen.queryByRole('list', { name: 'Checkouts no model uses' })).toBeNull()
  })

  it('clears a failed re-list once the next removal re-lists them (#3839)', async () => {
    setMockUnusedCheckouts([
      { name: 'BOSL2', commit: 'deadbeef'.repeat(5), used_by: [] },
      { name: 'threads', commit: 'c0ffee00'.repeat(5), used_by: [] },
    ])
    const store = storeDownOnRemoval('BOSL2')
    const { user } = renderPage(<LibraryUpgrade />)
    const list = await screen.findByRole('list', { name: 'Checkouts no model uses' })
    await user.click(within(list).getByRole('button', { name: 'Remove BOSL2 deadbee' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not re-list the checkouts')
    // The store is back for the second removal, whose re-list lands.
    store.down = false
    await user.click(within(list).getByRole('button', { name: 'Remove threads c0ffee0' }))
    await waitFor(() => expect(screen.queryByRole('list', { name: 'Checkouts no model uses' })).toBeNull())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it("says why a removal was refused, and keeps the row", async () => {
    setMockUnusedCheckouts([{ name: 'BOSL2', commit: 'deadbeef'.repeat(5), used_by: [] }])
    server.use(
      http.delete('/api/v1/libraries/:name', () => problem(409, 'Conflict', 'a running render reads BOSL2 deadbeef: job j1')),
    )
    const { user } = renderPage(<LibraryUpgrade />)
    const list = await screen.findByRole('list', { name: 'Checkouts no model uses' })
    await user.click(within(list).getByRole('button', { name: 'Remove BOSL2 deadbee' }))
    expect(await within(list).findByRole('alert')).toHaveTextContent('a running render reads BOSL2 deadbeef: job j1')
    expect(within(list).getByRole('listitem', { name: 'BOSL2 deadbee' })).toBeInTheDocument()
  })
})
