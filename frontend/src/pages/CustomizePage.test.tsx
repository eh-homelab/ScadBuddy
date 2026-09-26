import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { Route, Routes, useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { Job, PipelineChoices, Plate } from '../api/types'
import {
  pipelineViews,
  printOptions,
  settings as settingsFixture,
  targets,
  versionIds,
} from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { RENDER_DEBOUNCE_MS } from '../lib/useRenderJob'
import { CustomizePage } from './CustomizePage'

// WebGL does not exist in jsdom, so the canvas is replaced with a readable stand-in.
// The viewer itself is covered by the Playwright smoke test.
vi.mock('../components/Preview', () => ({
  Preview: ({ job, rendering, plate }: { job?: Job; rendering: boolean; plate?: Plate }) => (
    <div data-testid="preview">
      {rendering && <span>rendering</span>}
      {plate && (
        <span data-testid="plate">
          {plate.name} {plate.size[0]} × {plate.size[1]}
        </span>
      )}
      {job?.status === 'failed' && (
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
function watchRenders(): Promise<{ params: Record<string, unknown>; version?: string }>[] {
  const bodies: Promise<{ params: Record<string, unknown>; version?: string }>[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/render')) {
      bodies.push(
        request.clone().json() as Promise<{ params: Record<string, unknown>; version?: string }>,
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

  it('renders the defaults without being asked', async () => {
    render()
    await firstRender()
    expect(screen.getByTestId('bbox')).toHaveTextContent('64.1 × 37.2 × 6.8 mm')
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

  it('sends a generated output and links to the Bambuddy queue', async () => {
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Send to Bambuddy' }))
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    // A pipeline is configured in the fixtures, so the send starts a pipeline run
    // rather than queueing the plate itself.
    await waitFor(() => expect(within(dialog).getByText(/Pipeline run/)).toBeInTheDocument())
    expect(within(dialog).getByRole('button', { name: 'Open in queue' })).toBeInTheDocument()
    // A public URL is configured in the fixtures, so the note went on the file.
    expect(within(dialog).getByText(/Bambuddy has the link back/)).toBeInTheDocument()
  })

  it('says when no link back to the parameters was attached', async () => {
    server.use(
      http.post('/api/v1/outputs/:id/send', () =>
        HttpResponse.json({
          mode: 'queue',
          library_file_id: 41,
          filename: 'name-keychain-reagan.3mf',
          pipeline_run_id: 12,
          queue_item_id: null,
          bambuddy_url: 'https://bambuddy.test/queue',
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
          mode: 'library',
          library_file_id: 41,
          filename: 'name-keychain-reagan.3mf',
          pipeline_run_id: null,
          queue_item_id: null,
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

  it('sends the per-send print options the Options disclosure collected (#88)', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/outputs/:id/send', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({
          mode: 'queue',
          library_file_id: 41,
          filename: 'name-keychain.3mf',
          queue_item_id: 7,
          bambuddy_url: 'https://bambuddy.test/queue',
          options: { timelapse: true },
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
    await user.click(within(dialog).getByText('Options'))
    await waitFor(() => expect(within(dialog).getByLabelText('Timelapse')).toBeInTheDocument())
    await user.selectOptions(within(dialog).getByLabelText('Timelapse'), 'true')
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(bodies).toHaveLength(1))
    // No `copies` at all: the send bar's Copies box is `options.quantity` now, so there
    // is one control and one field rather than two that can disagree.
    expect(bodies[0]).toEqual({ mode: 'queue', options: { timelapse: true } })
  })

  it('does not let an untouched Copies box beat a remembered quantity (#88)', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ ...printOptions, models: { 'name-keychain': { quantity: 5 } } }),
      ),
    )
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/outputs/:id/send', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({
          mode: 'queue',
          library_file_id: 41,
          filename: 'name-keychain.3mf',
          queue_item_id: 7,
          bambuddy_url: 'https://bambuddy.test/queue',
          options: { quantity: 5 },
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
    // The box shows what will actually be printed, without claiming it as an override.
    await waitFor(() => expect(within(dialog).getByLabelText('Copies')).toHaveValue(5))
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(bodies).toHaveLength(1))
    // Nothing about quantity goes out, so the remembered 5 is what the server resolves.
    expect(bodies[0]).toEqual({ mode: 'queue', options: {} })
  })

  it('keeps the Copies box and the Options row on one value (#88)', async () => {
    server.use(
      http.get('/api/v1/settings/print-options', () =>
        HttpResponse.json({ ...printOptions, models: { 'name-keychain': { quantity: 5 } } }),
      ),
    )
    const bodies: unknown[] = []
    server.use(
      http.post('/api/v1/outputs/:id/send', async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({
          mode: 'queue',
          library_file_id: 41,
          filename: 'name-keychain.3mf',
          queue_item_id: 7,
          bambuddy_url: 'https://bambuddy.test/queue',
          options: { quantity: 2 },
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
    await waitFor(() => expect(within(dialog).getByLabelText('Copies')).toHaveValue(5))
    await user.click(within(dialog).getByText('Options'))
    await waitFor(() => expect(within(dialog).getByLabelText('Quantity')).toBeInTheDocument())

    // Editing Copies must not leave the disclosure's Quantity row showing the old number.
    // `fireEvent.change`, not `type`: the box clamps to its minimum on every keystroke,
    // so a cleared-then-typed value appends to the clamp rather than replacing it. A real
    // select-all-and-type produces exactly this one change event.
    fireEvent.change(within(dialog).getByLabelText('Copies'), { target: { value: '2' } })

    expect(within(dialog).getByLabelText('Quantity')).toHaveValue(2)
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ mode: 'queue', options: { quantity: 2 } })
  })

  it('reports the quantity the server resolved, not one guessed locally (#88)', async () => {
    // Never answers, so the disclosure's merge never lands and only the send result can
    // say what was queued — the shape of a Send that beats a slow Bambuddy.
    server.use(http.get('/api/v1/settings/print-options', () => new Promise(() => {})))
    server.use(
      http.post('/api/v1/outputs/:id/send', () =>
        HttpResponse.json({
          mode: 'queue',
          library_file_id: 41,
          filename: 'name-keychain.3mf',
          queue_item_id: 7,
          bambuddy_url: 'https://bambuddy.test/queue',
          options: { quantity: 4 },
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

    await waitFor(() => expect(within(dialog).getByText(/Queued as/)).toBeInTheDocument())
    expect(within(dialog).getByText(/copies\./)).toBeInTheDocument()
    expect(within(dialog).getByText('4')).toBeInTheDocument()
  })

  it('does not carry a per-send option into the next send (#88)', async () => {
    const bodies: Record<string, unknown>[] = []
    server.use(
      http.post('/api/v1/outputs/:id/send', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json({
          mode: 'queue',
          library_file_id: 41,
          filename: 'name-keychain.3mf',
          queue_item_id: 7,
          bambuddy_url: 'https://bambuddy.test/queue',
          options: {},
        })
      }),
    )
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    // First send, with an option set for this print only.
    await user.click(screen.getByRole('button', { name: 'Send to Bambuddy' }))
    let dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    await user.click(within(dialog).getByText('Options'))
    await waitFor(() =>
      expect(within(dialog).getByLabelText('Power off afterwards')).toBeInTheDocument(),
    )
    await user.selectOptions(within(dialog).getByLabelText('Power off afterwards'), 'true')
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]?.options).toEqual({ auto_off_after: true })
    await user.click(within(dialog).getByRole('button', { name: 'Done' }))

    // Second send, without touching the disclosure — it is collapsed, so a leaked
    // override would be invisible.
    await user.click(screen.getByRole('button', { name: 'Send to Bambuddy' }))
    dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    await user.click(within(dialog).getByRole('button', { name: 'Send' }))

    await waitFor(() => expect(bodies).toHaveLength(2))
    expect(bodies[1]?.options).toEqual({})
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
      if (body.params['name'] === 'Nova') expect(body.version).toBe(versionIds.added)
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
    // The Draft pipeline aims at a second printer, an A1 mini, so switching pipelines
    // switches printers — and plates.
    server.use(
      http.get('/api/v1/print/models/:slug/pipelines', () =>
        HttpResponse.json({
          pipelines: pipelineViews.map((pipeline) =>
            pipeline.id === 2
              ? { ...pipeline, target_printer_id: 2, target_printer_name: 'Mini', printer_ids: [2] }
              : pipeline,
          ),
          printers: [
            targets.printers![0]!,
            { id: 2, name: 'Mini', model: 'A1M', is_active: true, nozzle_count: 1 },
          ],
          model_pipeline_id: null,
          global_pipeline_id: 1,
          default_pipeline_id: 1,
        } satisfies PipelineChoices),
      ),
    )
    const { user } = render()
    await firstRender()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())

    // The default pipeline targets the H2C.
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
    await user.click(await within(again).findByRole('radio', { name: /Draft/ }))

    await waitFor(() => expect(screen.getByTestId('plate')).toHaveTextContent('A1 mini 180 × 180'))
    expect(screen.getByTestId('plate-fit')).toHaveTextContent(/X is 132\.1 mm over the A1 mini/)
    expect(screen.getByTestId('plate-fit')).toHaveTextContent(/Y is 105\.2 mm over the A1 mini/)
    expect(screen.getByTestId('print')).toHaveTextContent('Too big on X, Y')
    // Two generates, two picker opens and a debounced re-render.
  }, 20000)

  it('counts changes against the model defaults', async () => {
    const { user } = render()
    await firstRender()
    expect(screen.getByText('Defaults')).toBeInTheDocument()

    await user.clear(screen.getByRole('textbox', { name: 'Name on the tag' }))
    expect(screen.getByText('1 changed from defaults')).toBeInTheDocument()
  })
})
