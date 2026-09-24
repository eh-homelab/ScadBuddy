import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes, useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { Job } from '../api/types'
import { printOptions } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'

// WebGL does not exist in jsdom, so the canvas is replaced with a readable stand-in.
// The viewer itself is covered by the Playwright smoke test.
vi.mock('../components/Preview', () => ({
  Preview: ({ job, rendering }: { job?: Job; rendering: boolean }) => (
    <div data-testid="preview">
      {rendering && <span>rendering</span>}
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

async function firstRender() {
  await waitFor(() => expect(screen.getByTestId('bbox')).toBeInTheDocument(), { timeout: 4000 })
}

describe('CustomizePage', () => {
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

    await waitFor(() =>
      expect(within(dialog).getByText(/No link back to these parameters/)).toBeInTheDocument(),
    )
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

  it('counts changes against the model defaults', async () => {
    const { user } = render()
    await firstRender()
    expect(screen.getByText('Defaults')).toBeInTheDocument()

    await user.clear(screen.getByRole('textbox', { name: 'Name on the tag' }))
    expect(screen.getByText('1 changed from defaults')).toBeInTheDocument()
  })
})
