import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import type { ReactNode } from 'react'
import { Route, Routes, useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { BoundingBox, ChoicesView, Job, Plate } from '../api/types'
import { choicesView } from '../mocks/choices'
import {
  BUILTIN_SLUG,
  CANCELLED_ERROR,
  keychainSchema,
  projectViews,
  settings as settingsFixture,
  targets,
  versionIds,
} from '../mocks/fixtures'
import * as fixtures from '../mocks/fixtures'
import { setMockPlates } from '../mocks/handlers'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { COPY, duplicateWithUpdate, theirs } from '../test/upstream'
import { renderPage } from '../test/utils'
import { RENDER_DEBOUNCE_MS } from '../lib/useRenderJob'
import { CustomizePage } from './CustomizePage'

// WebGL does not exist in jsdom, so the canvas is replaced with a readable stand-in.
// The viewer itself is covered by the Playwright smoke test. The page's own buttons,
// which the viewer lays over the scene, are rendered as they are.
vi.mock('../components/Preview', () => ({
  Preview: ({
    job,
    rendering,
    plate,
    leading,
    controls,
  }: {
    job?: Job
    rendering: boolean
    plate?: Plate
    leading?: ReactNode
    controls?: ReactNode
  }) => (
    <div data-testid="preview">
      {leading}
      {controls}
      {rendering && <span>rendering</span>}
      {plate && (
        <span data-testid="plate">
          {plate.name} {plate.size[0]} × {plate.size[1]}
        </span>
      )}
      {(job?.status === 'failed' || job?.status === 'cancelled') && (
        <pre data-testid="render-log">{(job.log_tail ?? []).join('\n')}</pre>
      )}
      {job?.bbox_mm && (
        <span data-testid="bbox">
          {job.bbox_mm.size[0]} × {job.bbox_mm.size[1]} × {job.bbox_mm.size[2]} mm
        </span>
      )}
    </div>
  ),
}))

function render(route = '/m/name-keychain', state?: unknown) {
  return renderPage(<CustomizePage />, { route, path: '/m/:slug', state })
}

/** Reads back where the router ended up, so a redirect is observable. */
function Where() {
  const { pathname, search } = useLocation()
  return <div data-testid="where">{pathname + search}</div>
}

/**
 * Records the paths msw is asked for, so a route CHOICE can be asserted without an
 * override handler: one that re-fetched the same URL to stay transparent is
 * intercepted by msw again and recurses until the worker runs out of heap.
 */
function watchRequests(): string[] {
  const seen: string[] = []
  server.events.on('request:start', ({ request }) => seen.push(new URL(request.url).pathname))
  return seen
}

/** Records every render request's body, so what was ASKED of the server can be asserted. */
function watchRenders(): Promise<{ inputs: { params: Record<string, unknown> }; version?: string }>[] {
  const bodies: Promise<{ inputs: { params: Record<string, unknown> }; version?: string }>[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/render')) {
      bodies.push(
        request.clone().json() as Promise<{ inputs: { params: Record<string, unknown> }; version?: string }>,
      )
    }
  })
  return bodies
}

async function firstRender() {
  await waitFor(() => expect(screen.getByTestId('bbox')).toBeInTheDocument(), { timeout: 4000 })
}

describe('CustomizePage', () => {
  it('offers to delete the model, naming it', async () => {
    const { user } = render()
    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(screen.getByRole('dialog', { name: 'Delete Name Keychain?' })).toBeInTheDocument()
  })

  it('manages the template media from a Media dialog (#279)', async () => {
    const { user } = render()
    await user.click(await screen.findByRole('button', { name: 'Media' }))
    const dialog = screen.getByRole('dialog', { name: 'Media' })
    // The keychain's legacy thumbnail.png is its one item, and so its cover.
    const items = within(within(dialog).getByRole('list', { name: 'Media items' })).getAllByRole(
      'listitem',
    )
    expect(items).toHaveLength(1)
    expect(items[0]).toHaveTextContent('Cover')
    expect(within(dialog).getByLabelText('Add images or videos')).toBeInTheDocument()
  })

  it("opens a built-in's media to add to, what it ships read-only (#279, #722)", async () => {
    const { user } = render(`/m/${encodeURIComponent(BUILTIN_SLUG)}`)
    await user.click(await screen.findByRole('button', { name: 'Media' }))
    const dialog = screen.getByRole('dialog', { name: 'Media' })
    expect(within(dialog).getByText(/ships is read-only/)).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Add images or videos')).toBeInTheDocument()
  })

  it('offers to edit the model details (#179)', async () => {
    const { user } = render()
    await user.click(await screen.findByRole('button', { name: 'Edit details' }))
    const dialog = screen.getByRole('dialog', { name: 'Edit details' })
    expect(await within(dialog).findByLabelText('Name')).toHaveValue('Name Keychain')
  })

  it('takes a rename from Edit details into the heading and Duplicate without a reload (#179)', async () => {
    const { user } = render()
    await user.click(await screen.findByRole('button', { name: 'Edit details' }))
    const details = screen.getByRole('dialog', { name: 'Edit details' })
    const name = await within(details).findByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'Keyring')
    await user.click(within(details).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    // The still-open page's heading and Duplicate's prefill both take the new name.
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Keyring')
    await user.click(screen.getByRole('button', { name: 'Duplicate' }))
    const duplicate = screen.getByRole('dialog', { name: /^Duplicate / })
    expect(within(duplicate).getByLabelText('Name')).toHaveValue('Keyring copy')
  })

  it('renders the defaults without being asked', async () => {
    render()
    await firstRender()
    expect(screen.getByTestId('bbox')).toHaveTextContent('64.1 × 37.2 × 6.8 mm')
  })

  it('says the queue is full and renders anyway once the delay passes', async () => {
    server.use(
      http.post(
        '/api/v1/models/:slug/render',
        () =>
          HttpResponse.json(
            {
              type: 'about:blank',
              title: 'Service Unavailable',
              status: 503,
              detail: 'the render queue is full (16 jobs waiting for a worker); try again in 1 s',
              retry_after: 1,
            },
            { status: 503, headers: { 'Retry-After': '1' } },
          ),
        { once: true },
      ),
    )
    render()
    expect(await screen.findByTestId('render-busy', {}, { timeout: 4000 })).toHaveTextContent(
      'retried in 1 s',
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await firstRender()
    expect(screen.queryByTestId('render-busy')).not.toBeInTheDocument()
  })

  it('re-renders after a parameter change and updates the dimensions', async () => {
    const { user } = render()
    await firstRender()

    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'Nova')

    await waitFor(() => expect(screen.getByTestId('bbox')).toHaveTextContent('46.7'), {
      timeout: 4000,
    })
  })

  it('shows the OpenSCAD log when a render fails', async () => {
    const { user } = render()
    await firstRender()

    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'boom')

    const log = await screen.findByTestId('render-log', {}, { timeout: 4000 })
    expect(log).toHaveTextContent('Compilation failed')
  })

  it('shows the log when a render is cancelled, same as a failure, with the backend\'s own wording', async () => {
    // Preview is mocked above (its own copy for `cancelled` vs `failed` is covered
    // by Preview.test.tsx); this only checks the mock job store and useRenderJob
    // wiring carry the cancellation through, with the same text the real backend's
    // `CANCELLED_ERROR` uses rather than an OpenSCAD-shaped failure message.
    const { user } = render()
    await firstRender()

    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'superseded')

    const log = await screen.findByTestId('render-log', {}, { timeout: 4000 })
    expect(log).toHaveTextContent(CANCELLED_ERROR)
    expect(log).not.toHaveTextContent('Compilation failed')
  })

  it('disables Generate while a render is in flight', async () => {
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())

    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), '!')
    expect(screen.getByTestId('generate')).toBeDisabled()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled(), { timeout: 4000 })
  })

  it('generates an output, then enables download and send', async () => {
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())

    expect(screen.getByRole('button', { name: 'Download 3MF' })).toBeDisabled()
    await user.click(screen.getByTestId('generate'))

    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Download 3MF' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Send to Bambuddy' })).toBeEnabled()
  })

  it('says to allow pop-ups when the download popup is blocked inside Bambuddy (#612)', async () => {
    const top = window.top
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    try {
      const { user } = render()
      await firstRender()
      await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
      await user.click(screen.getByTestId('generate'))
      await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

      await user.click(screen.getByRole('button', { name: 'Download 3MF' }))
      expect(await screen.findByRole('alert')).toHaveTextContent(/Allow pop-ups/)
    } finally {
      open.mockRestore()
      Object.defineProperty(window, 'top', { value: top, configurable: true })
    }
  })

  it('says the download window was closed when it goes before the file is ready (#612)', async () => {
    const top = window.top
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    // Open when the click asks for it, closed by the time the 3MF has been fetched.
    let checks = 0
    const popup = {
      get closed() {
        checks += 1
        return checks > 1
      },
      document: null,
      close: () => {},
    }
    const open = vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window)
    try {
      const { user } = render()
      await firstRender()
      await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
      await user.click(screen.getByTestId('generate'))
      await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

      await user.click(screen.getByRole('button', { name: 'Download 3MF' }))
      expect(await screen.findByRole('alert')).toHaveTextContent(/download window was closed/)
    } finally {
      open.mockRestore()
      Object.defineProperty(window, 'top', { value: top, configurable: true })
    }
  })

  it('sends a generated output to the library and links to it', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/outputs/:id/send', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({
          library_file_id: 41,
          filename: 'name-keychain-reagan.3mf',
          bambuddy_url: 'https://bambuddy.test/library',
          edit_url: 'https://scad.test/edit/x',
        })
      }),
    )
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Send to Bambuddy' }))
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    // #312: the send bar only uploads. Queueing is the Print dialog's job.
    expect(within(dialog).queryByRole('radio')).not.toBeInTheDocument()
    expect(within(dialog).queryByLabelText('Copies')).not.toBeInTheDocument()
    expect(within(dialog).queryByText('Options')).not.toBeInTheDocument()
    expect(within(dialog).getByText(/use Print/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(within(dialog).getByText(/Added to the library/)).toBeInTheDocument())
    expect(bodies).toEqual([{ mode: 'library' }])
    expect(within(dialog).getByRole('button', { name: 'Open in library' })).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Open in queue' })).not.toBeInTheDocument()
    expect(within(dialog).getByText(/Bambuddy has the link back/)).toBeInTheDocument()
  })

  it('shows the refusal the server sent, and stays open to retry', async () => {
    server.use(
      http.post('/api/v1/outputs/:id/send', () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/plate-does-not-fit',
            title: 'Conflict',
            status: 409,
            detail: 'the model is 200 mm across and does not fit the A1 mini plate',
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Send to Bambuddy' }))
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('A1 mini'))
    expect(within(dialog).getByRole('button', { name: 'Send' })).toBeEnabled()
  })

  it('says when no link back to the parameters was attached', async () => {
    server.use(
      http.post('/api/v1/outputs/:id/send', () =>
        HttpResponse.json({
          library_file_id: 41,
          filename: 'name-keychain-reagan.3mf',
          bambuddy_url: 'https://bambuddy.test/library',
          edit_url: null,
        }),
      ),
    )
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Send to Bambuddy' }))
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    // A public URL is configured, so no link back means Bambuddy refused the note —
    // not that the feature was never switched on.
    await waitFor(() =>
      expect(within(dialog).getByText(/would not take the link/)).toBeInTheDocument(),
    )
  })

  it('says nothing about the link when no public URL is configured', async () => {
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({ ...settingsFixture, public_url: null }),
      ),
      http.post('/api/v1/outputs/:id/send', () =>
        HttpResponse.json({
          library_file_id: 41,
          filename: 'name-keychain-reagan.3mf',
          bambuddy_url: 'https://bambuddy.test/library',
          edit_url: null,
        }),
      ),
    )
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Send to Bambuddy' }))
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(within(dialog).getByText(/Added to the library/)).toBeInTheDocument())
    expect(within(dialog).queryByText(/link back to these parameters/)).not.toBeInTheDocument()
  })

  it('reopens an earlier output with its parameters', async () => {
    render(`/m/name-keychain?from=${'c'.repeat(32)}`)
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Nova'),
    )
    expect(screen.getByText('reopened from Nova')).toBeInTheDocument()
  })

  it('renders a pinned revision from its own schema, without restoring it', async () => {
    const seen = watchRequests()
    render(`/m/name-keychain?version=${versionIds.added}`)
    await firstRender()

    expect(screen.getByTestId('version-badge')).toHaveTextContent(
      `revision ${versionIds.added.slice(0, 7)}`,
    )
    expect(screen.getByRole('button', { name: 'Back to current' })).toBeInTheDocument()
    // The schema comes from the revision, never from the model's current source.
    expect(seen).toContain(`/api/v1/models/name-keychain/versions/${versionIds.added}/schema`)
    expect(seen).not.toContain('/api/v1/models/name-keychain/schema')
  })

  it('never renders a revision with the parameters of the one before it', async () => {
    const renders = watchRenders()
    const { user } = render(`/m/name-keychain?version=${versionIds.added}`)
    await firstRender()

    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'Nova')
    await waitFor(() => expect(screen.getByTestId('bbox')).toHaveTextContent('46.7'), {
      timeout: 4000,
    })

    await user.click(screen.getByRole('button', { name: 'Back to current' }))
    // Re-query: the form unmounts while the current schema is fetched, so the
    // node captured above detaches and keeps its old value for ever.
    await waitFor(
      () => expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Reagan'),
      { timeout: 4000 },
    )
    await firstRender()

    // The revision's parameters must never be paired with another revision's id:
    // `version` changes the instant the URL does, the schema and the values a
    // fetch and a debounce later. An intermediate request holding both renders
    // the wrong thing, and 422s outright when the two schemas differ.
    const bodies = await Promise.all(renders)
    expect(bodies.length).toBeGreaterThan(1)
    // 'Nova' was only ever typed into the pinned revision, so no request may
    // carry it once the page is back on the model's current source.
    for (const body of bodies) {
      if (body.inputs.params['name'] === 'Nova') expect(body.version).toBe(versionIds.added)
    }
    // Two debounced renders and a schema refetch do not fit the default budget.
  }, 20000)

  it('links to the versions panel', async () => {
    render()
    await firstRender()
    expect(screen.getByRole('link', { name: 'Versions' })).toHaveAttribute(
      'href',
      '/m/name-keychain/versions',
    )
  })

  it('re-reads the schema and re-renders once the model libraries change (#93)', async () => {
    const seen = watchRequests()
    const renders = watchRenders()
    const { user } = render()
    await firstRender()
    const schemaReads = () => seen.filter((path) => path.endsWith('/schema')).length
    const before = { schema: schemaReads(), renders: renders.length }

    await user.click(screen.getByRole('button', { name: 'Libraries' }))
    const bosl2 = await screen.findByRole('listitem', { name: 'BOSL2' })
    await user.click(within(bosl2).getByRole('button', { name: 'Add' }))
    await screen.findByRole('list', { name: 'Pinned libraries' })
    // Not under the open dialog: re-reading the schema re-mounts the page.
    expect(schemaReads()).toBe(before.schema)
    await user.click(screen.getByRole('button', { name: 'Done' }))

    await waitFor(() => expect(schemaReads()).toBe(before.schema + 1))
    await waitFor(() => expect(renders.length).toBe(before.renders + 1), { timeout: 4000 })
    await firstRender()
  })

  it('reopens from the 3MF alone when the output record is gone', async () => {
    const id = 'd'.repeat(32)
    server.use(
      http.get('/api/v1/outputs/:id/edit', () =>
        HttpResponse.json({
          output_id: id,
          slug: 'name-keychain',
          name: null,
          params: { name: 'Salvaged' },
          model_version: `sha256:${'ab'.repeat(32)}`,
          source: '3mf',
        }),
      ),
    )
    render(`/m/name-keychain?from=${id}`)
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Salvaged'),
    )
    expect(screen.getByText(`reopened from ${id.slice(0, 8)}`)).toBeInTheDocument()
  })

  it('uses the target EditPage already resolved rather than fetching it again', async () => {
    const id = 'c'.repeat(32)
    let calls = 0
    server.use(
      http.get('/api/v1/outputs/:outputId/edit', () => {
        calls += 1
        return HttpResponse.json({ title: 'Should not be called', status: 500 }, { status: 500 })
      }),
    )
    render(`/m/name-keychain?from=${id}`, {
      editTarget: {
        output_id: id,
        slug: 'name-keychain',
        name: 'Handed over',
        params: { name: 'Handed over' },
        model_version: null,
        source: 'record',
      },
    })
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Handed over'),
    )
    expect(calls).toBe(0)
  })

  it('still resolves the target when opened without it — a pasted link or a reload', async () => {
    const id = 'c'.repeat(32)
    render(`/m/name-keychain?from=${id}`)
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Nova'),
    )
  })

  it('sends a hand-typed link on to the model the output belongs to', async () => {
    // EditPage always builds the URL from the resolved slug, so only a typed or
    // bookmarked one can name the wrong model — and applying another model's
    // values to this schema silently is worse than moving to the right one.
    const id = 'c'.repeat(32)
    renderPage(
      <Routes>
        <Route
          path="/m/:slug"
          element={
            <>
              <Where />
              <CustomizePage />
            </>
          }
        />
      </Routes>,
      { route: `/m/some-other-model?from=${id}` },
    )
    await waitFor(() =>
      expect(screen.getByTestId('where')).toHaveTextContent(`/m/name-keychain?from=${id}`),
    )
  })

  it('never paints the schema defaults before a handed-over output\'s values', async () => {
    // The panel mounts with whatever `values` holds at that commit, so the value the
    // input carries when it first enters the DOM is the one the user would see.
    const id = 'c'.repeat(32)
    const first: string[] = []
    const observer = new MutationObserver(() => {
      const input = document.querySelector<HTMLInputElement>('input[type="text"]')
      if (input && first.length === 0) first.push(input.value)
    })
    observer.observe(document.body, { childList: true, subtree: true })

    render(`/m/name-keychain?from=${id}`, {
      editTarget: {
        output_id: id,
        slug: 'name-keychain',
        name: 'Handed over',
        params: { name: 'Handed over' },
        model_version: null,
        source: 'record',
      },
    })
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Handed over'),
    )
    observer.disconnect()
    expect(first).toEqual(['Handed over'])
  })

  it('does not open a blank customizer when the link is dead', async () => {
    // A pasted /m/{slug}?from={id} whose output and 3MF are both gone has to say so,
    // the way /edit/{id} does, rather than quietly showing a fresh model.
    const id = '0'.repeat(32)
    server.use(
      http.get('/api/v1/outputs/:outputId/edit', () =>
        HttpResponse.json({ title: 'Not found', status: 404 }, { status: 404 }),
      ),
    )
    renderPage(
      <Routes>
        <Route path="/m/:slug" element={<CustomizePage />} />
        <Route path="/edit/:outputId" element={<div data-testid="gone" />} />
      </Routes>,
      { route: `/m/name-keychain?from=${id}` },
    )
    expect(await screen.findByTestId('gone')).toBeInTheDocument()
  })

  it('renders nothing for a page it is only passing through', async () => {
    // The hooks run before the redirects below them, so without a guard the wrong
    // model gets a real OpenSCAD job — one render-concurrency slot for nothing.
    const id = 'c'.repeat(32)
    const rendered: string[] = []
    server.use(
      // Slower than the debounce on purpose: that is the window in which the page
      // holds a slug it is about to leave, and the only one where this can go wrong.
      http.get('/api/v1/outputs/:outputId/edit', async () => {
        await delay(RENDER_DEBOUNCE_MS * 2)
        return HttpResponse.json({
          output_id: id,
          slug: 'name-keychain',
          name: 'Nova',
          params: { name: 'Nova' },
          model_version: null,
          source: 'record',
        })
      }),
      http.post('/api/v1/models/:slug/render', ({ params }) => {
        rendered.push(String(params.slug))
        return HttpResponse.json({ job_id: `job-${String(params.slug)}` }, { status: 202 })
      }),
    )
    renderPage(
      <Routes>
        <Route path="/m/:slug" element={<CustomizePage />} />
      </Routes>,
      { route: `/m/some-other-model?from=${id}` },
    )
    await new Promise((resolve) => setTimeout(resolve, RENDER_DEBOUNCE_MS * 4))
    // The model the link actually belongs to may render; the one in the URL may not.
    expect(rendered).not.toContain('some-other-model')
  })

  it('draws the default plate while no printer has been chosen (#81)', async () => {
    render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('plate')).toHaveTextContent('Default plate 256 × 256'))
    expect(screen.queryByTestId('plate-fit')).not.toBeInTheDocument()
  })

  it('follows the printer chosen in the print picker, and warns when the model does not fit (#81)', async () => {
    // A second printer, an A1 mini: choosing it in the print dialog switches plates.
    server.use(
      http.get('/api/v1/print/outputs/:id/choices', ({ request }) => {
        const asked = new URL(request.url).searchParams.get('printer_id')
        return HttpResponse.json({
          ...choicesView,
          printer_id: asked === null ? 1 : Number(asked),
          printers: [
            targets.printers![0]!,
            { id: 2, name: 'Mini', model: 'A1M', is_active: true, nozzle_count: 1 },
          ],
        } satisfies ChoicesView)
      }),
    )
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    // The dialog opens on the H2C.
    await user.click(screen.getByTestId('print'))
    const dialog = await screen.findByRole('dialog', { name: 'Print' })
    await waitFor(() => expect(screen.getByTestId('plate')).toHaveTextContent('H2C 330 × 320'))
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    // 312.1 mm wide: on the 330 mm bed, but past the 300 mm both H2C nozzles reach.
    await user.click(screen.getByRole('tab', { name: 'Plate' }))
    const padding = screen.getByRole('spinbutton', { name: 'Margin around the text' })
    await user.clear(padding)
    await user.type(padding, '130')
    await waitFor(
      () =>
        expect(screen.getByTestId('plate-fit')).toHaveTextContent(
          'X is 12.1 mm over the H2C (312.1 of 300.0 mm)',
        ),
      { timeout: 4000 },
    )
    expect(screen.getByTestId('plate-fit')).not.toHaveTextContent(/Y is/)
    expect(screen.getByTestId('print')).toHaveTextContent('Too big on X')

    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByTestId('print')).toBeEnabled())
    await user.click(screen.getByTestId('print'))
    const again = await screen.findByRole('dialog', { name: 'Print' })
    await user.selectOptions(await within(again).findByLabelText('Printer'), '2')

    await waitFor(() => expect(screen.getByTestId('plate')).toHaveTextContent('A1 mini 180 × 180'))
    expect(screen.getByTestId('plate-fit')).toHaveTextContent(/X is 132\.1 mm over the A1 mini/)
    expect(screen.getByTestId('plate-fit')).toHaveTextContent(/Y is 105\.2 mm over the A1 mini/)
    expect(screen.getByTestId('print')).toHaveTextContent('Too big on X, Y')
    // Two generates, two picker opens and a debounced re-render.
  }, 20000)

  it('warns of what the send would refuse even when every axis fits (#81)', async () => {
    // The server runs the send's own placement; a box inside the reachable area can
    // still leave no room for a multi-colour print's prime tower.
    const asked: URLSearchParams[] = []
    server.use(
      http.get('/api/v1/plate/fit', ({ request }) => {
        asked.push(new URL(request.url).searchParams)
        return HttpResponse.json({
          plate: {
            model: null,
            name: 'Default plate',
            size: [256, 256],
            height: 250,
            usable: { min_x: 0, min_y: 0, max_x: 256, max_y: 256 },
          },
          overshoots: [],
          problem: 'the model is 64.1 x 37.2 mm, which leaves no room for the 60 mm prime tower',
        })
      }),
    )
    render()
    await firstRender()
    await waitFor(() =>
      expect(screen.getByTestId('plate-fit')).toHaveTextContent(/no room for the 60 mm prime tower/),
    )
    expect(screen.getByTestId('print')).toHaveTextContent('Does not fit')
    // Two colours, so the check is asked with the tower the send would add.
    expect(asked.at(-1)?.get('colours')).toBe('2')
    expect(asked.at(-1)?.get('x')).toBe('64.1')
  })

  it('checks a multi-plate render plate by plate, and names the plate that does not fit (#289)', async () => {
    const box = (x: number, y: number, z: number): BoundingBox => ({
      min: [0, 0, 0],
      max: [x, y, z],
      size: [x, y, z],
    })
    setMockPlates('name-keychain', [
      { index: 1, bbox_mm: box(244, 244, 8.5), colors: ['#111111', '#222222'] },
      { index: 2, bbox_mm: box(310, 248, 5.6), colors: ['#FFFFFF'] },
    ])
    const asked: URLSearchParams[] = []
    server.use(
      http.get('/api/v1/plate/fit', ({ request }) => {
        const search = new URL(request.url).searchParams
        asked.push(search)
        const x = Number(search.get('x'))
        return HttpResponse.json({
          plate: {
            model: null,
            name: 'Default plate',
            size: [256, 256],
            height: 250,
            usable: { min_x: 0, min_y: 0, max_x: 256, max_y: 256 },
          },
          overshoots: x > 256 ? [{ axis: 'X', size: x, limit: 256 }] : [],
          problem: null,
        })
      }),
    )
    render()
    await firstRender()
    await waitFor(() =>
      expect(screen.getByTestId('plate-fit')).toHaveTextContent(
        'Does not fit: plate 2: X is 54.0 mm over the default plate (310.0 of 256.0 mm).',
      ),
    )
    // Each plate with its own box and colours; never the preview's box of both.
    const checked = asked.map((search) => [search.get('x'), search.get('colours')])
    expect(checked).toContainEqual(['244', '2'])
    expect(checked).toContainEqual(['310', '1'])
    expect(checked.map(([x]) => x)).not.toContain('64.1')
    expect(screen.getByTestId('print')).toHaveTextContent('Too big on X')
    // The Print button's tooltip names the plate too, not only the banner.
    expect(screen.getByTestId('print')).toHaveAttribute(
      'title',
      'plate 2: X is 54.0 mm over the default plate (310.0 of 256.0 mm)',
    )
  })

  it('counts changes against the model defaults', async () => {
    const { user } = render()
    await firstRender()
    expect(screen.getByText('Defaults')).toBeInTheDocument()

    await user.clear(screen.getByRole('textbox', { name: 'Name on the tag' }))
    expect(screen.getByText('1 changed from defaults')).toBeInTheDocument()
  })

  it('offers the write actions on a model of the user\'s own', async () => {
    render()
    expect(await screen.findByRole('link', { name: 'Edit source' })).toHaveAttribute(
      'href',
      '/m/name-keychain/source',
    )
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
    expect(screen.queryByTestId('builtin-badge')).not.toBeInTheDocument()
  })

  it('says so when the model record fails to load, and a retry brings the write actions back', async () => {
    let fail = true
    server.use(
      http.get('/api/v1/models/:slug', () =>
        fail
          ? HttpResponse.json({ title: 'Data directory is unreadable', status: 500 }, { status: 500 })
          : undefined,
      ),
    )
    const { user } = render()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not load this model')
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Edit source' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit details' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('builtin-badge')).not.toBeInTheDocument()

    fail = false
    await user.click(within(alert).getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('button', { name: 'Delete' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Edit source' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit details' })).toBeInTheDocument()
    expect(screen.queryByText(/Could not load this model/)).not.toBeInTheDocument()
  })

  it('offers no write action on a built-in template, and says why (#184)', async () => {
    render(`/m/${encodeURIComponent(BUILTIN_SLUG)}`)
    expect(await screen.findByTestId('builtin-badge')).toHaveTextContent(
      'Built-in template — read-only',
    )
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Edit source' })).not.toBeInTheDocument()
    // #179: its details are the image's too.
    expect(screen.queryByRole('button', { name: 'Edit details' })).not.toBeInTheDocument()
  })

  it('still customizes a built-in template, and links its read-only pages encoded (#184)', async () => {
    render(`/m/${encodeURIComponent(BUILTIN_SLUG)}`)
    await firstRender()
    expect(screen.getByRole('heading', { name: 'Keychain Template' })).toBeInTheDocument()
    expect(await screen.findByRole('link', { name: 'View source' })).toHaveAttribute(
      'href',
      '/m/builtin%3Akeychain-template/source',
    )
    expect(screen.getByRole('link', { name: 'Versions' })).toHaveAttribute(
      'href',
      '/m/builtin%3Akeychain-template/versions',
    )
    expect(screen.getByRole('link', { name: /^History/ })).toHaveAttribute(
      'href',
      '/m/builtin%3Akeychain-template/history',
    )
  })

  it('duplicates a built-in from its header and opens the copy, linked to it (#159)', async () => {
    const { user } = render(`/m/${encodeURIComponent(BUILTIN_SLUG)}`)

    await user.click(await screen.findByRole('button', { name: 'Duplicate' }))
    const dialog = screen.getByRole('dialog', { name: 'Duplicate Keychain Template' })
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate' }))

    expect(
      await screen.findByRole('heading', { name: 'Keychain Template copy' }),
    ).toBeInTheDocument()
    expect(await screen.findByTestId('duplicated-from')).toHaveTextContent(
      `Duplicated from ${BUILTIN_SLUG}`,
    )
    expect(screen.getByRole('link', { name: BUILTIN_SLUG })).toHaveAttribute(
      'href',
      '/m/builtin%3Akeychain-template',
    )
    // The copy is the user's: its write actions are back.
    expect(await screen.findByRole('link', { name: 'Edit source' })).toBeInTheDocument()
    expect(screen.queryByTestId('builtin-badge')).not.toBeInTheDocument()
    // #179: Edit details among them, opening on the copy's own details.
    await user.click(screen.getByRole('button', { name: 'Edit details' }))
    const details = screen.getByRole('dialog', { name: 'Edit details' })
    expect(await within(details).findByLabelText('Name')).toHaveValue('Keychain Template copy')
  })

  it('offers Duplicate on a model of the user\'s own too (#159)', async () => {
    render()
    expect(await screen.findByRole('button', { name: 'Duplicate' })).toBeInTheDocument()
    expect(screen.queryByTestId('duplicated-from')).not.toBeInTheDocument()
  })

  it('takes an upstream update from its header badge, re-reading the schema (#160)', async () => {
    await duplicateWithUpdate()
    const seen = watchRequests()
    const { user } = render(`/m/${COPY}`)

    await user.click(await screen.findByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog', { name: 'Update available' })
    await within(dialog).findByTestId('merge-result')
    const schemaReads = seen.filter((path) => path.endsWith(`/${COPY}/schema`)).length
    await user.click(within(dialog).getByRole('button', { name: 'Take update' }))

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Update available' })).not.toBeInTheDocument(),
    )
    expect(await api.getSource(COPY)).toBe(theirs)
    // The merge changed the source, so the schema is read again.
    await waitFor(() =>
      expect(seen.filter((path) => path.endsWith(`/${COPY}/schema`)).length).toBe(schemaReads + 1),
    )
  })

  it('dismisses an update without re-reading the unchanged schema (#160)', async () => {
    await duplicateWithUpdate()
    const seen = watchRequests()
    const { user } = render(`/m/${COPY}`)

    await user.click(await screen.findByRole('button', { name: 'Update available' }))
    const dialog = screen.getByRole('dialog', { name: 'Update available' })
    await within(dialog).findByTestId('merge-result')
    const schemaReads = seen.filter((path) => path.endsWith(`/${COPY}/schema`)).length
    await user.click(within(dialog).getByRole('button', { name: 'Not now' }))

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Update available' })).not.toBeInTheDocument(),
    )
    expect((await api.getModel(COPY)).upstream_state).toBe('dismissed')
    expect(seen.filter((path) => path.endsWith(`/${COPY}/schema`))).toHaveLength(schemaReads)
  })

  it('shows no update badge on a model that is not a duplicate (#160)', async () => {
    render()
    await screen.findByRole('button', { name: 'Duplicate' })
    expect(screen.queryByTestId('update-badge')).not.toBeInTheDocument()
    expect(screen.queryByTestId('upstream-gone')).not.toBeInTheDocument()
  })
})

describe('CustomizePage, live (#269)', () => {
  /** The source changed elsewhere: the name's default is now 'Agent'. */
  function changeSourceElsewhere() {
    server.use(
      http.get('/api/v1/models/name-keychain/schema', () =>
        HttpResponse.json({
          ...keychainSchema,
          parameters: (keychainSchema.parameters ?? []).map((p) =>
            p.name === 'name' ? { ...p, initial: 'Agent' } : p,
          ),
        }),
      ),
    )
    emitRealtime('source.changed', ['model:name-keychain'], { slug: 'name-keychain' })
  }

  it('follows a source change made elsewhere when nothing has been edited', async () => {
    render()
    await firstRender()
    changeSourceElsewhere()
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Agent'),
    )
  })

  it('keeps edited values and asks before reloading the parameters', async () => {
    const { user } = render()
    await firstRender()
    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'Mine')

    changeSourceElsewhere()
    expect(await screen.findByText(/source changed elsewhere/)).toBeInTheDocument()
    expect(name).toHaveValue('Mine')

    await user.click(screen.getByRole('button', { name: 'Reload parameters' }))
    await waitFor(() => expect(name).toHaveValue('Agent'))
    expect(screen.queryByText(/source changed elsewhere/)).not.toBeInTheDocument()
  })

  it('does not carry the banner to the copy Duplicate opens', async () => {
    const { user } = render()
    await firstRender()
    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'Mine')
    changeSourceElsewhere()
    await screen.findByText(/source changed elsewhere/)

    await user.click(screen.getByRole('button', { name: 'Duplicate' }))
    const dialog = screen.getByRole('dialog', { name: /^Duplicate / })
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate' }))
    await screen.findByTestId('duplicated-from')
    expect(screen.queryByText(/source changed elsewhere/)).not.toBeInTheDocument()
  })

  it('shows details edited elsewhere without a reload', async () => {
    render()
    await screen.findByRole('heading', { name: 'Name Keychain' })
    await api.updateModel('name-keychain', { name: 'Renamed Elsewhere' })
    emitRealtime('model.updated', ['model:name-keychain', 'models'], { slug: 'name-keychain' })
    expect(await screen.findByRole('heading', { name: 'Renamed Elsewhere' })).toBeInTheDocument()
  })
})

// jsdom has no Fullscreen API, so these run the fallback: the workspace covers the
// window. The API itself is exercised by the Playwright run.
describe('full screen', () => {
  it('shows the view alone, with the parameters in a flyout', async () => {
    const { user } = render()
    await firstRender()
    const generate = screen.getByTestId('generate')

    await user.click(screen.getByRole('button', { name: 'Full screen' }))
    expect(screen.getByRole('button', { name: 'Exit full screen' })).toBeInTheDocument()
    // The parameters wait in the closed flyout, and the actions outside full screen.
    expect(screen.queryByRole('textbox', { name: 'Name on the tag' })).not.toBeInTheDocument()
    expect(generate).not.toBeVisible()

    const parameters = screen.getByRole('button', { name: 'Parameters' })
    expect(parameters).toHaveAttribute('aria-expanded', 'false')
    await user.click(parameters)
    expect(parameters).toHaveAttribute('aria-expanded', 'true')
    const close = screen.getByRole('button', { name: 'Close parameters' })
    expect(close).toHaveFocus()

    // A change made in the flyout renders like any other.
    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.clear(name)
    await user.type(name, 'Nova')
    await waitFor(() => expect(screen.getByTestId('bbox')).toHaveTextContent('46.7'), {
      timeout: 4000,
    })

    await user.click(close)
    expect(screen.queryByRole('textbox', { name: 'Name on the tag' })).not.toBeInTheDocument()
    expect(parameters).toHaveFocus()

    await user.click(screen.getByRole('button', { name: 'Exit full screen' }))
    expect(screen.queryByRole('button', { name: 'Parameters' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Close parameters' })).not.toBeInTheDocument()
    // Back in its column with the change, never remounted.
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Nova')
    expect(generate).toBeVisible()
  })

  it('takes the page around the view out of reach while full screen', async () => {
    const { user } = render()
    await firstRender()
    const versions = screen.getByRole('link', { name: 'Versions' })

    await user.click(screen.getByRole('button', { name: 'Full screen' }))
    // Covered, so Tab must not reach it and navigate away unseen.
    expect(versions.closest('[inert]')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Exit full screen' }).closest('[inert]')).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Exit full screen' }))
    expect(versions.closest('[inert]')).toBeNull()
  })

  it('opens each full screen on the view alone', async () => {
    const { user } = render()
    await firstRender()
    await user.click(screen.getByRole('button', { name: 'Full screen' }))
    await user.click(screen.getByRole('button', { name: 'Parameters' }))
    await user.keyboard('{Escape}')
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Full screen' }))
    expect(screen.getByRole('button', { name: 'Parameters' })).toHaveAttribute(
      'aria-expanded',
      'false',
    )
    expect(screen.queryByRole('textbox', { name: 'Name on the tag' })).not.toBeInTheDocument()
  })

  it('lets Escape close the font picker in the flyout without leaving the stand-in', async () => {
    const { user } = render()
    await firstRender()
    await user.click(screen.getByRole('button', { name: 'Full screen' }))
    await user.click(screen.getByRole('button', { name: 'Parameters' }))
    await user.click(screen.getByRole('button', { name: 'Browse' }))
    expect(screen.getByRole('dialog', { name: 'Choose a font' })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Exit full screen' })).toBeInTheDocument()

    // With nothing else to take it, the next one leaves full screen.
    await user.keyboard('{Escape}')
    expect(screen.getByRole('button', { name: 'Full screen' })).toBeInTheDocument()
  })
})

describe('CustomizePage, project file (#317)', () => {
  /** The last send went to `Reagan Keychain` (1), so both pickers open on it. */
  function withLastProject(projectId: number | null) {
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json({ projects: projectViews, last_project_id: projectId }),
      ),
    )
  }

  /** The bodies of every request to a path ending in `suffix`, by method. */
  /** Bodies of the `method` requests whose path ends with `path` (a string) or matches it. */
  function watchBodies(method: string, path: string | RegExp): Promise<unknown>[] {
    const bodies: Promise<unknown>[] = []
    server.events.on('request:start', ({ request }) => {
      const pathname = new URL(request.url).pathname
      const matches = typeof path === 'string' ? pathname.endsWith(path) : path.test(pathname)
      if (request.method === method && matches) {
        bodies.push(request.clone().json())
      }
    })
    return bodies
  }

  function pagePicker(): HTMLSelectElement {
    return screen.getByTestId('customize-project-select')
  }

  async function generate(user: ReturnType<typeof render>['user']) {
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved [0-9a-f]/)).toBeInTheDocument())
  }

  it('opens the picker on the last project', async () => {
    withLastProject(1)
    render()
    await waitFor(() => expect(pagePicker()).toHaveValue('1'))
  })

  it('files the generated output in the chosen project and links to it', async () => {
    withLastProject(1)
    const filed = watchBodies('POST', '/project-file')
    const { user } = render()
    await waitFor(() => expect(pagePicker()).toHaveValue('1'))
    await generate(user)

    const status = await screen.findByTestId('project-filed')
    expect(status).toHaveTextContent('Saved to Reagan Keychain')
    expect(within(status).getByRole('button', { name: 'Open in Bambuddy' })).toBeInTheDocument()
    expect(filed).toHaveLength(1)
    expect(await filed[0]).toEqual({ project_id: 1 })
  })

  it('uploads nothing on Generate with "No project", and remembers that choice', async () => {
    withLastProject(1)
    const filed = watchBodies('POST', '/project-file')
    const remembered = watchBodies('PUT', '/print/projects/last')
    const { user } = render()
    await waitFor(() => expect(pagePicker()).toHaveValue('1'))
    await user.selectOptions(pagePicker(), '')
    await waitFor(() => expect(remembered).toHaveLength(1))
    expect(await remembered[0]).toEqual({ project_id: null })

    await generate(user)
    expect(filed).toHaveLength(0)
    expect(screen.queryByTestId('project-filed')).not.toBeInTheDocument()
  })

  it('keeps Print disabled until the project file is filed, so the print reuses it', async () => {
    withLastProject(1)
    let answer: (() => void) | undefined
    const answered = new Promise<void>((resolve) => {
      answer = resolve
    })
    server.use(
      http.post('/api/v1/outputs/:id/project-file', async () => {
        await answered
        return HttpResponse.json({
          project_id: 1,
          folder_id: 9,
          library_file_id: 41,
          filename: 'Keychain.3mf',
          created: true,
          bambuddy_url: 'http://bambuddy.local/projects/1',
        })
      }),
    )
    const { user } = render()
    await waitFor(() => expect(pagePicker()).toHaveValue('1'))
    await generate(user)

    expect(screen.getByTestId('print')).toBeDisabled()
    answer?.()
    await screen.findByTestId('project-filed')
    expect(screen.getByTestId('print')).toBeEnabled()
  })

  it('shares one choice with the print dialog', async () => {
    withLastProject(null)
    const { user } = render()
    await waitFor(() => expect(pagePicker()).toHaveValue(''))
    await user.selectOptions(pagePicker(), '2')
    await generate(user)

    await waitFor(() => expect(screen.getByTestId('print')).toBeEnabled())
    await user.click(screen.getByTestId('print'))
    const dialog = await screen.findByRole('dialog')
    // #768 — the project is an Advanced step in the dialog; the page's own picker is not.
    await user.click(await within(dialog).findByRole('switch', { name: 'Advanced' }))
    const dialogPicker = await within(dialog).findByTestId('project-select')
    expect(dialogPicker).toHaveValue('2')

    await user.selectOptions(dialogPicker, '1')
    expect(pagePicker()).toHaveValue('1')
  })

  it('keeps a project created in the dialog while the remembered one is still the old one', async () => {
    // The list keeps answering with the old project as the last one, as it does while
    // the PUT that remembers the new choice has not landed (it never answers here).
    withLastProject(1)
    server.use(http.put('/api/v1/print/projects/last', () => new Promise<never>(() => undefined)))
    let listed = 0
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'GET' && new URL(request.url).pathname === '/api/v1/print/projects') {
        listed += 1
      }
    })
    const { user } = render()
    await waitFor(() => expect(pagePicker()).toHaveValue('1'))
    await generate(user)

    await waitFor(() => expect(screen.getByTestId('print')).toBeEnabled())
    await user.click(screen.getByTestId('print'))
    const dialog = await screen.findByRole('dialog')
    // #768 — the project is an Advanced step in the dialog; the page's own picker is not.
    await user.click(await within(dialog).findByRole('switch', { name: 'Advanced' }))
    const dialogPicker = await within(dialog).findByTestId<HTMLSelectElement>('project-select')
    await user.selectOptions(dialogPicker, 'new')
    await user.type(within(dialog).getByTestId('new-project-name'), 'Workshop Bins')
    await user.click(within(dialog).getByTestId('create-project'))

    await waitFor(() => expect(dialogPicker.selectedOptions[0]).toHaveTextContent(/Workshop Bins/))
    const created = dialogPicker.value
    expect(created).not.toBe('1')
    expect(pagePicker()).toHaveValue(created)
    // Neither picker snaps back to the remembered project on a later re-read.
    await new Promise((resolve) => setTimeout(resolve, 200))
    expect(pagePicker()).toHaveValue(created)
    expect(dialogPicker).toHaveValue(created)
    // One list for the page and the dialog, not one each.
    expect(listed).toBe(1)
  })

  it('prints with "No project" even while remembering it has not landed', async () => {
    withLastProject(1)
    // The PUT that remembers the choice never answers: the run alone must carry it.
    server.use(http.put('/api/v1/print/projects/last', () => new Promise<never>(() => undefined)))
    // The print run only: the dialog's analyzers also POST to `/analyzers/run` (#563).
    const ran = watchBodies('POST', /\/print\/outputs\/[^/]+\/run$/)
    const { user } = render()
    await waitFor(() => expect(pagePicker()).toHaveValue('1'))
    await user.selectOptions(pagePicker(), '')
    await generate(user)

    await waitFor(() => expect(screen.getByTestId('print')).toBeEnabled())
    await user.click(screen.getByTestId('print'))
    const dialog = await screen.findByRole('dialog')
    const print = await within(dialog).findByTestId('run-print')
    await waitFor(() => expect(print).toBeEnabled())
    await user.click(print)

    await waitFor(() => expect(ran).toHaveLength(1))
    expect(await ran[0]).toHaveProperty('project_id', null)
  })
})

describe('template inputs (spec 2026-09-27 §4.3)', () => {
  it('reopens an output with its UI state and saves it again with the output', async () => {
    const outputId = 'c'.repeat(32)
    server.use(
      http.get(`/api/v1/outputs/${outputId}/edit`, () =>
        HttpResponse.json({
          output_id: outputId,
          slug: 'name-keychain',
          name: 'Tagged',
          params: { name: 'Kai' },
          inputs: { params: { name: 'Kai' }, tab: 'lid', v: 0 },
          model_version: null,
          source: 'record',
        }),
      ),
    )
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/models/:slug/outputs', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ ...fixtures.outputs[0], id: 'd'.repeat(32) }, { status: 201 })
      }),
    )
    const { user } = render(`/m/name-keychain?from=${outputId}`)
    const generate = await screen.findByTestId('generate')
    await waitFor(() => expect(generate).toBeEnabled(), { timeout: 5000 })
    await user.click(generate)
    await waitFor(() =>
      expect(bodies[0]).toMatchObject({ inputs: { params: { name: 'Kai' }, tab: 'lid', v: 0 } }),
    )
  })
})
