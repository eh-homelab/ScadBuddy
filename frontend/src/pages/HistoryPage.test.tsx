import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import type userEvent from '@testing-library/user-event'
import { delay, HttpResponse, http } from 'msw'
import { Route, Routes, useLocation, useParams } from 'react-router'
import { beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import type { Output } from '../api/types'
import { bbox, keychainSchema, models, outputs, prints } from '../mocks/fixtures'
import { addMockOutput, problem } from '../mocks/handlers'
import { server } from '../mocks/server'
import { fakeRealtime } from '../lib/realtime.fake'
import { setDisplayUnit } from '../lib/units'
import { choose } from '../test/select'
import { intersect } from '../test/intersection'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'
import { EditPage } from './EditPage'
import type * as TraceAction from '../lib/traceAction'

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
import { HistoryPage } from './HistoryPage'
import { libraryLabel, pinChange } from '../lib/libraryPins'

// Each test's actions only (#2892): `toContain` would otherwise pass on an earlier test's.
beforeEach(() => {
  actions.length = 0
})

/**
 * The plates on `name`'s row once it is scrolled into view. The row is found and brought
 * into view again on every try: a slow CI shard can re-render it between the two, and a
 * row rendered anew has a fresh, unseen placeholder (CI, 2026-10-14).
 */
async function shownPlates(name: string) {
  return await waitFor(
    async () => {
      const item = await row(name)
      intersect(item)
      return within(item).getByRole('figure', { name: 'Plates' })
    },
    { timeout: 5000 },
  )
}

function render() {
  return renderPage(
    <Routes>
      <Route path="/m/:slug/history" element={<HistoryPage />} />
      <Route path="/edit/:outputId" element={<EditRoute />} />
    </Routes>,
    { route: '/m/name-keychain/history' },
  )
}

/** Stands in for the deep-link page so the test can read what it was given. */
function EditRoute() {
  const { outputId } = useParams()
  const state = useLocation().state as { editTarget?: { name?: string | null } } | null
  return <div data-testid="edit-route">{`${outputId ?? ''}:${state?.editTarget?.name ?? 'no-state'}`}</div>
}

/**
 * Rows are labelled by the output's name — its id is 32 hex characters. The name is
 * also one of the parameters, so it appears twice in its own row; the heading is first.
 */
/** Opens `item`'s "More for …" menu and returns its entry named `name` (#3672). */
async function menuItem(user: ReturnType<typeof userEvent.setup>, item: HTMLElement, name: string | RegExp): Promise<HTMLElement> {
  await user.click(within(item).getByRole('button', { name: /^More for / }))
  return screen.findByRole('menuitem', { name })
}

async function row(name: string): Promise<HTMLElement> {
  const headings = await screen.findAllByText(name)
  return (headings[0] as HTMLElement).closest('li') as HTMLElement
}

/** No print on record: a row says only where its file went. */
function noPrints() {
  server.use(http.get('/api/v1/prints', () => HttpResponse.json({ items: [], next_cursor: null })))
}

describe('HistoryPage', () => {
  it('shows a pipeline output\'s bill of materials and links its files', async () => {
    const first = outputs[0]!
    server.use(
      http.get('/api/v1/models/name-keychain/outputs', () =>
        HttpResponse.json([
          {
            ...first,
            bom: [{ piece: 'wall', label: 'Wall', count: 8, plates: [1, 2], part: 'k1' }],
            files: ['guide.svg'],
          },
        ]),
      ),
    )
    render()
    const item = await row('Reagan')
    const table = within(item).getByRole('table')
    expect(within(table).getAllByRole('row')[1]).toHaveTextContent('Wall81, 2')
    // #2287 — a button through downloadBlob, not a plain link the frame would drop.
    expect(within(item).getByRole('button', { name: 'guide.svg' })).toBeInTheDocument()
    expect(within(item).queryByRole('link', { name: 'guide.svg' })).not.toBeInTheDocument()
  })

  it('downloads a pipeline output\'s file through the escape popup when embedded (#612, #2287)', async () => {
    const first = outputs[0]!
    server.use(
      http.get('/api/v1/models/name-keychain/outputs', () =>
        HttpResponse.json([{ ...first, bom: [], files: ['guide.svg'] }]),
      ),
    )
    const top = window.top
    // Inside Bambuddy's iframe: its sandbox has no allow-downloads (CLAUDE.md).
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    // A blocked popup: downloadBlob refuses rather than start a download the frame drops.
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      const { user } = render()
      await user.click(within(await row('Reagan')).getByRole('button', { name: 'guide.svg' }))
      expect(open).toHaveBeenCalledWith('', '_blank')
      expect(await screen.findByRole('alert')).toHaveTextContent('Allow pop-ups')
    } finally {
      open.mockRestore()
      Object.defineProperty(window, 'top', { value: top, configurable: true })
    }
  })

  it("downloads a pipeline output's file into the escape popup, by its name, when embedded (#2546)", async () => {
    const first = outputs[0]!
    server.use(
      http.get('/api/v1/models/name-keychain/outputs', () =>
        HttpResponse.json([{ ...first, bom: [], files: ['guide.svg'] }]),
      ),
      http.get(`/api/v1/outputs/${first.id}/files/guide.svg`, () => HttpResponse.text('<svg/>')),
    )
    const top = window.top
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    const popup = document.implementation.createHTMLDocument('popup')
    const open = vi.spyOn(window, 'open').mockReturnValue({ document: popup, close: vi.fn() } as unknown as Window)
    const objectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:guide')
    let clicked: HTMLAnchorElement | undefined
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      // eslint-disable-next-line @typescript-eslint/no-this-alias -- the spy's receiver is the anchor under test
      clicked = this
    })
    try {
      const { user } = render()
      await user.click(within(await row('Reagan')).getByRole('button', { name: 'guide.svg' }))
      expect(open).toHaveBeenCalledWith('', '_blank')
      await waitFor(() => expect(clicked).toBeDefined())
      expect(clicked?.ownerDocument).toBe(popup)
      expect(clicked?.getAttribute('download')).toBe('guide.svg')
      expect(clicked?.href).toBe('blob:guide')
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    } finally {
      click.mockRestore()
      objectUrl.mockRestore()
      open.mockRestore()
      Object.defineProperty(window, 'top', { value: top, configurable: true })
    }
  })

  it("names the model in the breadcrumb, not OpenSCAD's customizer title (#939)", async () => {
    // OpenSCAD titles the schema after the .scad file it exported, so every model's
    // reads "model"; the record's name is the one the reader knows it by.
    server.use(
      http.get('/api/v1/models/:slug/schema', () => HttpResponse.json({ ...keychainSchema, title: 'model' })),
    )
    render()
    await screen.findByTestId('outputs')
    expect(screen.getByRole('link', { name: 'Name Keychain' })).toHaveAttribute('href', '/m/name-keychain')
    expect(screen.queryByRole('link', { name: 'model' })).not.toBeInTheDocument()
  })

  it('lists every output newest first', async () => {
    render()
    const list = await screen.findByTestId('outputs')
    expect(list.children).toHaveLength(3)
    expect(within(list.children[0] as HTMLElement).getByText('Reagan')).toBeInTheDocument()
  })

  it('shows each output’s size in the display unit', async () => {
    render()
    const nova = await row('Nova')
    expect(within(nova).getByText(/^[\d.]+ × [\d.]+ × [\d.]+ mm$/)).toBeInTheDocument()
    act(() => setDisplayUnit('in'))
    // Awaited: the row reads the unit through useSyncExternalStore, which subscribes in a
    // passive effect. findAllByText can resolve on the commit before that effect has run,
    // and then the change is not heard inside act; the store's check on subscribe
    // re-renders the row a moment later, outside it.
    expect(await within(nova).findByText(/^\d+\.\d\d × \d+\.\d\d × \d+\.\d\d in$/)).toBeInTheDocument()
  })

  it('diffs each output against the model defaults', async () => {
    render()
    const nova = await row('Nova')

    const textSize = within(nova).getByText('Text size').parentElement as HTMLElement
    expect(textSize).toHaveTextContent('18')
    expect(textSize).toHaveTextContent('14')
  })

  it('says so when an output used the defaults', async () => {
    server.use(
      http.get('/api/v1/models/:slug/outputs', () =>
        HttpResponse.json([
          {
            id: '9'.repeat(32),
            slug: 'name-keychain',
            name: 'Defaults',
            job_id: '8'.repeat(32),
            created_at: '2026-09-22T10:00:00Z',
            has_thumbnail: false,
            params: {},
            bbox_mm: bbox(10, 10, 10),
            colors: ['#1B6CA8'],
          },
        ]),
      ),
    )
    render()
    expect(await screen.findByText('Model defaults, unchanged.')).toBeInTheDocument()
  })

  it('shows where an output already went, by Bambuddy id', async () => {
    noPrints()
    render()
    expect(within(await row('Reagan')).getByText('queued #4471')).toBeInTheDocument()
    expect(within(await row('Nova')).getByText('in library #8790')).toBeInTheDocument()
  })

  it('deep-links each recorded id into Bambuddy', async () => {
    noPrints()
    render()
    // The base comes from Settings, because an output record carries ids and no URL.
    const queued = within(await row('Reagan')).getByRole('link', { name: 'queued #4471' })
    expect(queued).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/queue/4471',
    )
    expect(within(await row('Nova')).getByRole('link', { name: 'in library #8790' })).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/library',
    )
  })

  it('deep-links every plate of a multi-plate print', async () => {
    server.use(
      http.get('/api/v1/models/:slug/outputs', () =>
        HttpResponse.json([
          {
            id: '9'.repeat(32),
            slug: 'name-keychain',
            name: 'Plates',
            job_id: '8'.repeat(32),
            created_at: '2026-09-22T10:00:00Z',
            has_thumbnail: false,
            params: {},
            bbox_mm: bbox(10, 10, 10),
            colors: ['#1B6CA8'],
            queue_item_id: 72,
            slice_job_id: 10,
            plates: [
              { plate_id: 1, queue_item_id: 71, slice_job_id: 9 },
              { plate_id: 2, queue_item_id: 72, slice_job_id: 10 },
            ],
          },
        ]),
      ),
      // #986: a plate is named by what it holds; one with no name keeps its number.
      http.get('/api/v1/outputs/:id/plates', () =>
        HttpResponse.json([
          { index: 1, has_thumbnail: true, name: 'Lid' },
          { index: 2, has_thumbnail: true, name: null },
        ]),
      ),
    )
    render()
    const plates = await row('Plates')
    expect(await within(plates).findByRole('link', { name: 'Lid queued #71' })).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/queue/71',
    )
    expect(within(plates).getByRole('link', { name: 'Plate 2 queued #72' })).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/queue/72',
    )
    expect(within(plates).queryByText('queued #72')).not.toBeInTheDocument()
  })

  // #2117: an unreadable plate list leaves each plate's number, and the row stands.
  it('keeps the plate numbers when the plates cannot be read', async () => {
    let read = false
    server.use(
      http.get('/api/v1/models/:slug/outputs', () =>
        HttpResponse.json([
          {
            id: '9'.repeat(32),
            slug: 'name-keychain',
            name: 'Plates',
            job_id: '8'.repeat(32),
            created_at: '2026-09-22T10:00:00Z',
            has_thumbnail: false,
            params: {},
            bbox_mm: bbox(10, 10, 10),
            colors: ['#1B6CA8'],
            queue_item_id: 72,
            slice_job_id: 10,
            plates: [
              { plate_id: 1, queue_item_id: 71, slice_job_id: 9 },
              { plate_id: 2, queue_item_id: 72, slice_job_id: 10 },
            ],
          },
        ]),
      ),
      http.get('/api/v1/outputs/:id/plates', () => {
        read = true
        return HttpResponse.json({ detail: 'Bambuddy is down' }, { status: 500 })
      }),
    )
    render()
    const plates = await row('Plates')
    await waitFor(() => expect(read).toBe(true))
    expect(within(plates).getByRole('link', { name: 'Plate 1 queued #71' })).toBeInTheDocument()
    expect(within(plates).getByRole('link', { name: 'Plate 2 queued #72' })).toBeInTheDocument()
    expect(within(plates).getByRole('button', { name: /^Edit / })).toBeInTheDocument()
    expect(screen.queryByText(/Bambuddy is down/)).not.toBeInTheDocument()
  })

  it('edits an output through its deep link', async () => {
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Edit / }))
    expect(await screen.findByTestId('edit-route')).toHaveTextContent('c'.repeat(32))
  })

  it('hands the row it already rendered over rather than making it be resolved again', async () => {
    const { user } = render()
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Edit / }))
    expect(await screen.findByTestId('edit-route')).toHaveTextContent(':Nova')
  })

  it('asks before deleting an output, even one with no copies in Bambuddy (#2369)', async () => {
    const deleted: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'DELETE') deleted.push(new URL(request.url).pathname)
    })
    const { user } = render()
    await user.click(await menuItem(user, await row('Workshop'), /^Delete /))

    const dialog = await screen.findByRole('alertdialog', { name: 'Delete Workshop?' })
    expect(dialog).toHaveTextContent('This cannot be undone.')
    expect(within(dialog).queryByRole('list', { name: 'Library copies' })).not.toBeInTheDocument()
    expect(deleted).toEqual([])
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(within(await row('Workshop')).getByRole('button', { name: 'More for Workshop' })).toBeInTheDocument()
    expect(deleted).toEqual([])

    await user.click(await menuItem(user, await row('Workshop'), /^Delete /))
    await user.click(
      within(await screen.findByRole('alertdialog', { name: 'Delete Workshop?' })).getByRole('button', {
        name: 'Delete output',
      }),
    )
    await waitFor(() => expect(screen.queryAllByText('Workshop')).toHaveLength(0))
    expect(screen.getByTestId('outputs').children).toHaveLength(2)
    expect(actions).toContain('delete')
  })

  it('asks before deleting an output with copies in Bambuddy, and keeps project copies', async () => {
    let asked: string | null = null
    server.use(
      http.delete('/api/v1/outputs/:id', ({ request }) => {
        asked = new URL(request.url).search
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = render()
    await user.click(await menuItem(user, await row('Nova'), /^Delete /))

    const dialog = await screen.findByRole('alertdialog', { name: 'Delete Nova?' })
    const copies = within(dialog).getByRole('list', { name: 'Library copies' })
    expect(copies).toHaveTextContent('#8790 in the inbox folder')
    expect(copies).toHaveTextContent('#8789 in a project folder, kept')
    await user.click(
      within(dialog).getByRole('checkbox', { name: 'Also delete the inbox copies in Bambuddy' }),
    )
    await user.click(within(dialog).getByRole('button', { name: 'Delete output' }))

    await waitFor(() => expect(screen.queryAllByText('Nova')).toHaveLength(0))
    expect(asked).toBe('?delete_inbox_copies=true')
  })

  it('does not label a copy inbox or project before the settings have loaded', async () => {
    server.use(http.get('/api/v1/settings', () => delay('infinite')))
    const { user } = render()
    await user.click(await menuItem(user, await row('Nova'), /^Delete /))

    const dialog = await screen.findByRole('alertdialog', { name: 'Delete Nova?' })
    const copies = within(dialog).getByRole('list', { name: 'Library copies' })
    expect(copies).not.toHaveTextContent('in the inbox folder')
    expect(copies).not.toHaveTextContent('in a project folder')
    expect(within(copies).getAllByText(/folder not recorded/)).toHaveLength(2)
  })

  it('leaves Bambuddy alone unless the inbox copies are ticked', async () => {
    let asked: string | null = null
    server.use(
      http.delete('/api/v1/outputs/:id', ({ request }) => {
        asked = new URL(request.url).search
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const { user } = render()
    await user.click(await menuItem(user, await row('Nova'), /^Delete /))
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete Nova?' })
    await user.click(within(dialog).getByRole('button', { name: 'Delete output' }))

    await waitFor(() => expect(screen.queryAllByText('Nova')).toHaveLength(0))
    expect(asked).toBe('')
  })

  it('keeps the output and says why when Bambuddy refuses the delete', async () => {
    server.use(
      http.delete('/api/v1/outputs/:id', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Bad Gateway', status: 502, detail: 'Bambuddy said no' },
          { status: 502, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = render()
    await user.click(await menuItem(user, await row('Nova'), /^Delete /))
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete Nova?' })
    await user.click(within(dialog).getByRole('button', { name: 'Delete output' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Bambuddy said no')
    expect(screen.getByTestId('outputs').children).toHaveLength(3)
  })

  it('invites a first render when there is no history', async () => {
    server.use(http.get('/api/v1/models/:slug/outputs', () => HttpResponse.json([])))
    render()
    expect(await screen.findByRole('heading', { name: 'Nothing generated yet' })).toBeInTheDocument()
  })

  it('offers to send an output, "again" only once it was sent (#2287)', async () => {
    const { user } = render()
    expect(within(await row('Reagan')).getByRole('button', { name: 'Send again Reagan' })).toBeInTheDocument()
    const workshop = await row('Workshop')
    expect(within(workshop).queryByRole('button', { name: /^Send again / })).not.toBeInTheDocument()
    await user.click(within(workshop).getByRole('button', { name: 'Send Workshop' }))

    // #312: the send bar only uploads. There is no mode to choose any more.
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    expect(within(dialog).queryByRole('radio')).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Send' })).toBeInTheDocument()
  })

  it('arranges the outputs ticked on the page', async () => {
    const { user } = render()
    const reagan = await row('Reagan')
    expect(screen.getByRole('button', { name: 'Arrange selected (0)' })).toBeDisabled()
    await user.click(within(reagan).getByRole('checkbox', { name: 'Select Reagan' }))
    await user.click(screen.getByRole('button', { name: 'Arrange selected (1)' }))
    expect(await screen.findByLabelText('Copies of wall — Reagan')).toHaveValue(2)
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() =>
      expect(screen.queryByLabelText('Copies of wall — Reagan')).not.toBeInTheDocument(),
    )
    // The list reloads with the arranged output, and the selection is cleared.
    expect(await screen.findByText('Arranged from 1 output')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Arrange selected (0)' })).toBeDisabled()
    // #2287 — the next step, right there: it has not been sent, and Print is the way on.
    const next = screen.getByRole('group', { name: 'Next step' })
    expect(next).toHaveTextContent('Not sent to Bambuddy yet')
    expect(within(next).getByRole('button', { name: 'Download' })).toBeInTheDocument()
    await user.click(within(next).getByRole('button', { name: 'Print' }))
    expect(await screen.findByRole('dialog', { name: 'Print' })).toBeInTheDocument()
  })

  it("shows the arranged result's plates once, in its next step, not on its row too (#2754 review)", async () => {
    server.use(http.get('/api/v1/outputs/:id/plates', () => HttpResponse.json([{ index: 1, has_thumbnail: true }])))
    const { user } = render()
    await user.click(within(await row('Reagan')).getByRole('checkbox', { name: 'Select Reagan' }))
    await user.click(screen.getByRole('button', { name: 'Arrange selected (1)' }))
    await user.click(await screen.findByRole('button', { name: 'Arrange' }))
    await screen.findByRole('group', { name: 'Next step' })
    const arranged = (await screen.findByText('Arranged from 1 output')).closest('li')!
    expect(await screen.findByRole('figure', { name: 'Plates' })).toBeInTheDocument()
    expect(screen.getAllByRole('figure', { name: 'Plates' })).toHaveLength(1)
    expect(within(arranged).queryByRole('figure', { name: 'Plates' })).not.toBeInTheDocument()
  })

  it('keeps the list up while it reads it again after an arrange (#2283)', async () => {
    let slow = false
    server.use(
      http.get('/api/v1/models/name-keychain/outputs', async () => {
        if (slow) await delay(3000)
        return undefined // the default handler answers
      }),
    )
    const { user } = render()
    await user.click(within(await row('Reagan')).getByRole('checkbox', { name: 'Select Reagan' }))
    await user.click(screen.getByRole('button', { name: 'Arrange selected (1)' }))
    const arrange = await screen.findByRole('button', { name: 'Arrange' })
    slow = true
    await user.click(arrange)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Arrange' })).not.toBeInTheDocument())
    expect(screen.queryByText('Loading history')).not.toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Arranged' })).toBeInTheDocument()
    expect(screen.getAllByText('Reagan').length).toBeGreaterThan(0)
    expect(await screen.findByText('Arranged from 1 output', {}, { timeout: 5000 })).toBeInTheDocument()
  })

  it('says the result was saved here, with no link to where it already is (#2542)', async () => {
    const { user } = render()
    await user.click(within(await row('Reagan')).getByRole('checkbox', { name: 'Select Reagan' }))
    await user.click(screen.getByRole('button', { name: 'Arrange selected (1)' }))
    await user.click(await screen.findByRole('button', { name: 'Arrange' }))
    const note = await screen.findByRole('status', { name: 'Arranged' })
    expect(note).toHaveTextContent(/in Name Keychain's history\.$/)
    expect(within(note).queryByRole('link', { name: 'Open in History' })).not.toBeInTheDocument()
  })

  it("says \"this model's\" history, not its slug, when the model list cannot be read (#2541)", async () => {
    let failed = false
    server.use(
      http.get('/api/v1/models', () => {
        failed = true
        return problem(503, 'Service Unavailable', 'down')
      }),
    )
    const { user } = render()
    await user.click(within(await row('Reagan')).getByRole('checkbox', { name: 'Select Reagan' }))
    await user.click(screen.getByRole('button', { name: 'Arrange selected (1)' }))
    await waitFor(() => expect(failed).toBe(true))
    await user.click(await screen.findByRole('button', { name: 'Arrange' }))
    const note = await screen.findByRole('status', { name: 'Arranged' })
    expect(note).toHaveTextContent(/in this model's history\.$/)
    expect(note).not.toHaveTextContent('name-keychain')
  })

  it('says which model the result was filed under, and links to its History (#2542)', async () => {
    const first = outputs[0]!
    const bin: Output = {
      ...first,
      id: '9'.repeat(32),
      slug: 'gridfinity-bin',
      name: 'Bin',
      manifest: [{ ...first.manifest![0]!, part: 'piece-bin', slug: 'gridfinity-bin', bom_piece: 'bin', count: 1 }],
    }
    addMockOutput(bin)
    const { user } = render()
    await user.click(within(await row('Reagan')).getByRole('checkbox', { name: 'Select Reagan' }))
    await user.click(screen.getByRole('button', { name: 'Arrange selected (1)' }))
    await user.click(await screen.findByRole('button', { name: 'Add from a model' }))
    const binName = models.find((m) => m.slug === 'gridfinity-bin')!.name!
    const model = await screen.findByLabelText('Model')
    await waitFor(() => expect(model).toBeEnabled())
    await choose(user, model, binName)
    await user.click(await screen.findByRole('checkbox', { name: /^Bin · / }))
    await user.click(screen.getByRole('button', { name: 'Add (1)' }))
    expect(await screen.findByLabelText('Copies of bin — Bin')).toBeVisible()
    await choose(user, screen.getByLabelText('File under'), binName)
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    const note = await screen.findByRole('status', { name: 'Arranged' })
    expect(note).toHaveTextContent(`in ${binName}'s history.`)
    expect(within(note).getByRole('link', { name: 'Open in History' })).toHaveAttribute(
      'href',
      '/m/gridfinity-bin/history',
    )
  })

  it("says how an output's last print went, linked to its print, in place of the queue id (#2288)", async () => {
    render()
    // Reagan's newest print (archive 36) failed; its earlier one (35) succeeded.
    const reagan = await row('Reagan')
    const outcome = await within(reagan).findByRole('link', { name: 'Last print: Failed' })
    expect(outcome).toHaveAttribute('href', '/prints/36')
    expect(outcome).not.toHaveClass('text-ok')
    expect(within(reagan).queryByText('queued #4471')).not.toBeInTheDocument()
    // Nova's is still printing; Workshop's archive is gone from Bambuddy.
    expect(await within(await row('Nova')).findByRole('link', { name: 'Last print: Printing' })).toHaveAttribute(
      'href',
      '/prints/37',
    )
    expect(
      await within(await row('Workshop')).findByRole('link', { name: 'Last print: Deleted in Bambuddy' }),
    ).toBeInTheDocument()
  })

  it('says a print that finishes while the page is open, without a reload (#2786 review)', async () => {
    // Real time still passes, so the page's own waits run; `signal` advances the fakes.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    onTestFinished(() => {
      vi.useRealTimers()
    })
    const realtime = fakeRealtime({ confirm: false })
    const nova = outputs.find((o) => o.name === 'Nova')!
    const print = prints.find((p) => p.archive_id === 37)!
    const was = print.status
    onTestFinished(() => {
      print.status = was
    })
    render()
    const row_ = await row('Nova')
    expect(await within(row_).findByRole('link', { name: 'Last print: Printing' })).toBeInTheDocument()
    await waitFor(() => expect(realtime.following()).toContain(`print:${nova.id}`))
    print.status = 'failed'
    await realtime.signal(`print:${nova.id}`, 'print.settled')
    expect(await within(row_).findByRole('link', { name: 'Last print: Failed' })).toBeInTheDocument()
  })

  it("reads the prints again when one settles, not on its progress ticks (#2786 review)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    onTestFinished(() => {
      vi.useRealTimers()
    })
    const realtime = fakeRealtime({ confirm: false })
    const reads: string[] = []
    const listen = ({ request }: { request: Request }) => {
      if (request.method === 'GET' && new URL(request.url).pathname === '/api/v1/prints') reads.push(request.url)
    }
    server.events.on('request:start', listen)
    onTestFinished(() => {
      server.events.removeListener('request:start', listen)
    })
    const nova = outputs.find((o) => o.name === 'Nova')!
    render()
    const row_ = await row('Nova')
    expect(await within(row_).findByRole('link', { name: 'Last print: Printing' })).toBeInTheDocument()
    await waitFor(() => expect(realtime.following()).toContain(`print:${nova.id}`))
    const before = reads.length
    await realtime.signal(`print:${nova.id}`, 'print.progress')
    await realtime.signal(`print:${nova.id}`, 'print.progress')
    expect(reads).toHaveLength(before)
    await realtime.signal(`print:${nova.id}`, 'print.settled')
    await waitFor(() => expect(reads).toHaveLength(before + 1))
  })

  it('keeps the rows when the prints cannot be read (#2288)', async () => {
    server.use(http.get('/api/v1/prints', () => problem(503, 'Bambuddy is unreachable')))
    render()
    expect(await within(await row('Reagan')).findByText('queued #4471')).toBeInTheDocument()
    expect(screen.queryByText(/Last print/)).not.toBeInTheDocument()
  })

  it('shows a preview of every output that has one, larger on a click (#2288)', async () => {
    const { user } = render()
    const reagan = await row('Reagan')
    const preview = within(reagan).getByRole('button', { name: 'Preview Reagan' })
    // Decorative inside its button, which carries the name.
    expect(preview.querySelector('img')).toHaveAttribute('src', `/api/v1/outputs/${outputs[0]!.id}/thumbnail`)
    await user.click(preview)
    const dialog = await screen.findByRole('dialog', { name: 'Reagan' })
    expect(within(dialog).getByRole('img', { name: 'Preview of Reagan' })).toBeInTheDocument()
    // One with no thumbnail says so rather than leaving a gap.
    const without = outputs.find((o) => !o.has_thumbnail && o.name)!
    expect(within(await row(without.name!)).getByLabelText(`No preview of ${without.name!}`)).toBeInTheDocument()
  })

  it('offers Queue, Download 3MF and Rename on every output (#2288)', async () => {
    const { user } = render()
    for (const name of ['Reagan', 'Nova', 'Workshop']) {
      const item = await row(name)
      expect(within(item).getByRole('button', { name: 'Download 3MF' })).toBeInTheDocument()
      expect(await menuItem(user, item, `Queue ${name}`)).toBeInTheDocument()
      expect(screen.getByRole('menuitem', { name: `Rename ${name}` })).toBeInTheDocument()
      await user.keyboard('{Escape}')
    }
    await user.click(await menuItem(user, await row('Workshop'), 'Queue Workshop'))
    const dialog = await screen.findByRole('dialog', { name: 'Queue' })
    expect(within(dialog).getByTestId('run-print')).toHaveTextContent('Queue')
  })

  it('opens Print as Print after a Queue was closed (#2288)', async () => {
    const { user } = render()
    await user.click(await menuItem(user, await row('Workshop'), 'Queue Workshop'))
    await user.click(within(await screen.findByRole('dialog', { name: 'Queue' })).getByRole('button', { name: 'Cancel' }))
    await user.click(within(await row('Workshop')).getByRole('button', { name: 'Print Workshop' }))
    expect(await screen.findByRole('dialog', { name: 'Print' })).toBeInTheDocument()
  })

  it('renames an output, and a blank name gives it back its short id (#2288)', async () => {
    const { user } = render()
    await user.click(await menuItem(user, await row('Workshop'), 'Rename Workshop'))
    const field = screen.getByRole('textbox', { name: 'Name for Workshop' })
    expect(field).toHaveValue('Workshop')
    await user.clear(field)
    await user.type(field, 'Shed sign{Enter}')
    const renamed = await row('Shed sign')
    expect(screen.queryByRole('textbox', { name: 'Name for Workshop' })).not.toBeInTheDocument()
    // The server has it, not only the row.
    const id = outputs.find((o) => o.name === 'Workshop')!.id
    expect(((await (await fetch(`/api/v1/outputs/${id}`)).json()) as Output).name).toBe('Shed sign')

    await user.click(await menuItem(user, renamed, 'Rename Shed sign'))
    await user.clear(screen.getByRole('textbox', { name: 'Name for Shed sign' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(id.slice(0, 8))).toBeInTheDocument()
  })

  it('keeps a row deleted while a rename was in flight deleted (#2786 review)', async () => {
    let answer!: () => void
    const answered = new Promise<void>((resolve) => {
      answer = resolve
    })
    const id = outputs.find((o) => o.name === 'Workshop')!.id
    server.use(
      http.patch('/api/v1/outputs/:id', async () => {
        await answered
        return HttpResponse.json({ ...outputs.find((o) => o.id === id)!, name: 'Shed sign' })
      }),
      http.delete('/api/v1/outputs/:id', () => new HttpResponse(null, { status: 204 })),
    )
    const { user } = render()
    await user.click(await menuItem(user, await row('Workshop'), 'Rename Workshop'))
    const field = screen.getByRole('textbox', { name: 'Name for Workshop' })
    await user.clear(field)
    await user.type(field, 'Shed sign{Enter}')
    await user.click(await menuItem(user, await row('Nova'), /^Delete /))
    await user.click(
      within(await screen.findByRole('alertdialog', { name: 'Delete Nova?' })).getByRole('button', {
        name: 'Delete output',
      }),
    )
    await waitFor(() => expect(screen.queryAllByText('Nova')).toHaveLength(0))
    answer()
    expect(await row('Shed sign')).toBeInTheDocument()
    expect(screen.queryAllByText('Nova')).toHaveLength(0)
  })

  it('keeps the name being typed and says why when a rename fails (#2288)', async () => {
    server.use(http.patch('/api/v1/outputs/:id', () => problem(503, 'the volume is not writable')))
    const { user } = render()
    await user.click(await menuItem(user, await row('Workshop'), 'Rename Workshop'))
    await user.type(screen.getByRole('textbox', { name: 'Name for Workshop' }), ' 2')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('the volume is not writable')
    expect(screen.getByRole('textbox', { name: 'Name for Workshop' })).toHaveValue('Workshop 2')
  })

  it('offers Print on every output (#2287)', async () => {
    const { user } = render()
    await user.click(within(await row('Workshop')).getByRole('button', { name: 'Print Workshop' }))
    expect(await screen.findByRole('dialog', { name: 'Print' })).toBeInTheDocument()
  })

  it('says which outputs could not be re-rendered after the dialog closes (#902)', async () => {
    const workshop = outputs[2]!
    server.use(
      http.post(`/api/v1/outputs/${workshop.id}/backfill`, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Unprocessable Content', status: 422, detail: 'revision abc is gone' },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = render()
    await user.click(within(await row('Reagan')).getByRole('checkbox', { name: 'Select Reagan' }))
    await user.click(within(await row('Workshop')).getByRole('checkbox', { name: 'Select Workshop' }))
    await user.click(screen.getByRole('button', { name: 'Arrange selected (2)' }))
    await user.click(await screen.findByRole('button', { name: 'Arrange' }))
    await user.click(screen.getByRole('button', { name: 'Re-render' }))
    // Reagan still arranges; the dialog closes, and History says Workshop was skipped.
    expect(await screen.findByText('Arranged from 1 output', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Arrange' })).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Workshop could not be re-rendered: revision abc is gone. It was left out of the arrange.',
    )
  })

  it('drops a deleted output from the selection', async () => {
    const { user } = render()
    const workshop = await row('Workshop')
    await user.click(within(workshop).getByRole('checkbox', { name: 'Select Workshop' }))
    expect(screen.getByRole('button', { name: 'Arrange selected (1)' })).toBeEnabled()
    await user.click(await menuItem(user, workshop, 'Delete Workshop'))
    const dialog = await screen.findByRole('alertdialog', { name: 'Delete Workshop?' })
    await user.click(within(dialog).getByRole('button', { name: 'Delete output' }))
    await waitFor(() => expect(screen.queryAllByText('Workshop')).toHaveLength(0))
    expect(screen.getByRole('button', { name: 'Arrange selected (0)' })).toBeDisabled()
  })

  it('offers no Edit on an arranged output', async () => {
    server.use(
      http.get('/api/v1/models/name-keychain/outputs', () =>
        HttpResponse.json([
          {
            ...outputs[0],
            id: 'f'.repeat(32),
            name: 'Batch',
            params: {},
            arranged_from: ['a'.repeat(32), 'c'.repeat(32), 'library:89'],
            // #2287 — an arrange's result, never sent anywhere.
            library_files: [],
            queue_item_id: null,
            plates: [],
            print_route: null,
          },
        ]),
      ),
    )
    render()
    const batch = await row('Batch')
    expect(within(batch).queryByRole('button', { name: 'Edit Batch' })).not.toBeInTheDocument()
    // #2284 — every source counted, library files too.
    expect(within(batch).getByText('Arranged from 2 outputs and 1 library file')).toBeInTheDocument()
    expect(within(batch).getByRole('button', { name: 'Print Batch' })).toBeInTheDocument()
    expect(within(batch).getByRole('button', { name: 'Send Batch' })).toBeInTheDocument()
  })

  it('shows an arranged output\'s plates on its row, and a rendered one\'s not (#2282)', async () => {
    const arranged = { ...outputs[0], id: 'f'.repeat(32), name: 'Batch', arranged_from: ['a'.repeat(32), 'c'.repeat(32)] }
    const read: string[] = []
    server.use(
      http.get('/api/v1/models/name-keychain/outputs', () => HttpResponse.json([arranged, outputs[1]])),
      http.get('/api/v1/outputs/:id/plates', ({ params }) => {
        read.push(String(params['id']))
        return HttpResponse.json([
          { index: 1, has_thumbnail: true },
          { index: 2, has_thumbnail: false, name: 'tags' },
        ])
      }),
    )
    render()
    const batch = await row('Batch')
    // #2754 review — read once the row nears the viewport, not as it mounts.
    expect(within(batch).getByTestId('arranged-plates-pending')).toBeInTheDocument()
    expect(read).toEqual([])
    const plates = await shownPlates('Batch')
    expect(plates).toHaveTextContent('2 plates')
    expect(within(plates).getByRole('img', { name: 'Plate 1' })).toHaveAttribute(
      'src',
      `/api/v1/outputs/${'f'.repeat(32)}/plates/1/thumbnail`,
    )
    expect(plates).toHaveTextContent('Plate 2: tags')
    expect(within(await row('Nova')).queryByRole('figure', { name: 'Plates' })).not.toBeInTheDocument()
    // Only the arranged output's plates are read. Not "read once": run after the file's
    // other tests they are read twice (CI, 2026-10-14), which is no part of what this
    // test is about.
    expect(new Set(read)).toEqual(new Set(['f'.repeat(32)]))
  })

  it('shows a plate whose picture fails to load as one with none (#2754 review)', async () => {
    const arranged = { ...outputs[0], id: 'f'.repeat(32), name: 'Batch', arranged_from: ['a'.repeat(32), 'c'.repeat(32)] }
    server.use(
      http.get('/api/v1/models/name-keychain/outputs', () => HttpResponse.json([arranged])),
      http.get('/api/v1/outputs/:id/plates', () => HttpResponse.json([{ index: 1, has_thumbnail: true }])),
    )
    render()
    const plates = await shownPlates('Batch')
    fireEvent.error(within(plates).getByRole('img', { name: 'Plate 1' }))
    expect(within(plates).queryByRole('img')).not.toBeInTheDocument()
    expect(plates).toHaveTextContent('Plate 1')
  })
})

describe('template inputs (spec 2026-09-27 §4.3)', () => {
  it('Edit on a row keeps its UI state, and Generate saves it again', async () => {
    const withUi = outputs.map((output) =>
      output.name === 'Nova'
        ? { ...output, inputs: { params: output.params, tab: 'lid', v: 0 } }
        : output,
    )
    const bodies: unknown[] = []
    server.use(
      http.get('/api/v1/models/:slug/outputs', () => HttpResponse.json(withUi)),
      // The row is handed over; a fetch of the edit target would hide a dropped field.
      http.get('/api/v1/outputs/:id/edit', () => new HttpResponse(null, { status: 500 })),
      http.post('/api/v1/models/:slug/outputs', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ ...withUi[0], id: 'd'.repeat(32) }, { status: 201 })
      }),
    )
    const { user } = renderPage(
      <Routes>
        <Route path="/m/:slug/history" element={<HistoryPage />} />
        <Route path="/edit/:outputId" element={<EditPage />} />
        <Route path="/m/:slug" element={<CustomizePage />} />
      </Routes>,
      { route: '/m/name-keychain/history' },
    )
    await user.click(within(await row('Nova')).getByRole('button', { name: /^Edit / }))
    const generate = await screen.findByTestId('generate')
    await waitFor(() => expect(generate).toBeEnabled(), { timeout: 5000 })
    await user.click(generate)
    await waitFor(() => expect(bodies[0]).toMatchObject({ inputs: { tab: 'lid', v: 0 } }))
  }, 15000)
})

describe('HistoryPage, item context (#975)', () => {
  it("names each row's actions after its output", async () => {
    const { user } = render()
    const nova = await row('Nova')
    expect(within(nova).getByRole('button', { name: 'Edit Nova' })).toBeInTheDocument()
    expect(within(nova).getByRole('button', { name: 'Send again Nova' })).toBeInTheDocument()
    expect(await menuItem(user, nova, 'Delete Nova')).toBeInTheDocument()
  })

  it('reads a changed value as the value and its default, not the two run together', async () => {
    render()
    const nova = await row('Nova')
    const values = [...nova.querySelectorAll('dd')].map((dd) => dd.textContent)
    expect(values.length).toBeGreaterThan(0)
    for (const value of values) expect(value).toMatch(/^.+, default .+$/)
    // The old value is marked up as a deletion, not only struck through by CSS.
    expect(nova.querySelector('dd del')).not.toBeNull()
  })
})

describe('HistoryPage · libraries an output was rendered with (#1296)', () => {
  const dotscad = {
    name: 'dotSCAD',
    ref: 'v3.3',
    url: 'https://github.com/JustinSDK/dotSCAD',
    commit: 'bb33edf0123456789abcdef0123456789abcdef0',
  }

  function serve(outputLibraries: object[], modelLibraries: object[]) {
    const first = outputs[0]!
    server.use(
      http.get('/api/v1/models/name-keychain/outputs', () =>
        HttpResponse.json([{ ...first, libraries: outputLibraries }]),
      ),
      http.get('/api/v1/models/name-keychain', () =>
        HttpResponse.json({ ...models.find((m) => m.slug === 'name-keychain'), libraries: modelLibraries }),
      ),
    )
  }

  it('names each library and its short commit', async () => {
    serve([dotscad], [dotscad])
    render()
    const item = await row('Reagan')
    const line = within(item).getByLabelText('Rendered with libraries')
    expect(line).toHaveTextContent('dotSCAD v3.3 · bb33edf')
    expect(line).not.toHaveTextContent('now')
  })

  it("flags a library the model now pins at another commit", async () => {
    serve([dotscad], [{ ...dotscad, ref: 'v3.4', commit: 'c0ffee1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }])
    render()
    const item = await row('Reagan')
    await waitFor(() =>
      expect(within(item).getByLabelText('Rendered with libraries')).toHaveTextContent(
        'dotSCAD v3.3 · bb33edf (now dotSCAD v3.4 · c0ffee1)',
      ),
    )
  })

  // #2109: the line is read (and copied) as text, so its parts need real separators.
  it('separates a library from its warning and from the next library in the text', async () => {
    const bosl = { name: 'BOSL2', ref: 'v2.0', url: 'https://github.com/BelfrySCAD/BOSL2', commit: 'aaaa1110123456789abcdef0123456789abcdef0' }
    serve([dotscad, bosl], [{ ...dotscad, ref: 'v3.4', commit: 'c0ffee1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }, bosl])
    render()
    const item = await row('Reagan')
    await waitFor(() =>
      expect(within(item).getByLabelText('Rendered with libraries').textContent).toBe(
        'dotSCAD v3.3 · bb33edf (now dotSCAD v3.4 · c0ffee1), BOSL2 v2.0 · aaaa111',
      ),
    )
  })

  it('shows no line for an output rendered with no library', async () => {
    serve([], [dotscad])
    render()
    const item = await row('Reagan')
    expect(within(item).queryByLabelText('Rendered with libraries')).toBeNull()
  })

  it('flags nothing until the model is known, and a dropped pin once it is', () => {
    expect(libraryLabel(dotscad)).toBe('dotSCAD v3.3 · bb33edf')
    expect(pinChange(dotscad, undefined)).toBeNull()
    expect(pinChange(dotscad, [])).toBe('no longer pinned')
  })
})
