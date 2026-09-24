import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
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

function render(route = '/m/name-keychain') {
  return renderPage(<CustomizePage />, { route, path: '/m/:slug' })
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

  it('reopens an earlier output with its parameters', async () => {
    render(`/m/name-keychain?from=${'c'.repeat(32)}`)
    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Nova'),
    )
    expect(screen.getByText('reopened from Nova')).toBeInTheDocument()
  })

  it('counts changes against the model defaults', async () => {
    const { user } = render()
    await firstRender()
    expect(screen.getByText('Defaults')).toBeInTheDocument()

    await user.clear(screen.getByRole('textbox', { name: 'Name on the tag' }))
    expect(screen.getByText('1 changed from defaults')).toBeInTheDocument()
  })
})
