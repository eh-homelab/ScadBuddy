import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { bridge } from '../agent/bridge'
import { useGlobalAgentTools } from '../agent/global'
import { api } from '../api/client'
import type { Job } from '../api/types'
import { RENDER_DEBOUNCE_MS } from '../lib/useRenderJob'
import { projectViews } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'

// As in CustomizePage.test.tsx: jsdom has no WebGL.
vi.mock('../components/Preview', () => ({
  Preview: ({ job }: { job?: Job }) => (
    <div data-testid="preview">
      {job?.bbox_mm && <span data-testid="bbox">{job.bbox_mm.size.join(' × ')}</span>}
    </div>
  ),
}))

function Shell({ children }: { children: ReactNode }) {
  useGlobalAgentTools()
  return <>{children}</>
}

function watchRenders(): Record<string, unknown>[] {
  const bodies: Record<string, unknown>[] = []
  server.events.on('request:start', async ({ request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/render')) {
      bodies.push(((await request.clone().json()) as { inputs: { params: Record<string, unknown> } }).inputs.params)
    }
  })
  return bodies
}

async function open(route = '/m/name-keychain') {
  const view = renderPage(
    <Shell>
      <CustomizePage />
    </Shell>,
    { route, path: '/m/:slug' },
  )
  await waitFor(() => expect(screen.getByTestId('bbox')).toBeInTheDocument(), { timeout: 4000 })
  return view
}

async function call(name: string, args: unknown = {}) {
  return bridge.call(name, args)
}

describe('customizer tools', () => {
  it('reads the parameters with their types, limits and values', async () => {
    await open()
    const outcome = await call('get_params')
    expect(outcome.ok).toBe(true)
    if (!outcome.ok) return
    const { params, changed } = outcome.result as {
      params: { name: string; type: string; value: unknown; min?: number; max?: number }[]
      changed: string[]
    }
    expect(params.find((param) => param.name === 'text_size')).toMatchObject({
      type: 'slider',
      value: 14,
      min: 6,
      max: 28,
    })
    expect(changed).toEqual([])
  })

  it('set_param goes through the field: same value on screen, same debounced render', async () => {
    const renders = watchRenders()
    await open()
    await waitFor(() => expect(renders).toHaveLength(1))

    const outcome = await call('set_param', { name: 'name', value: 'Nova' })
    expect(outcome).toMatchObject({ ok: true, result: { name: 'name', value: 'Nova' } })
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Nova')

    // Nothing is submitted until the debounce a keystroke waits for has passed…
    await new Promise((resolve) => setTimeout(resolve, RENDER_DEBOUNCE_MS / 2))
    expect(renders).toHaveLength(1)
    // …and then exactly one render, of the new value.
    await waitFor(() => expect(renders).toHaveLength(2))
    expect(renders[1]).toMatchObject({ name: 'Nova' })

    // `render` waits for that render — not the previous one still on screen.
    const rendered = await call('render', { timeout_ms: 5000 })
    expect(rendered).toMatchObject({ ok: true, result: { status: 'done' } })
    const bbox = (rendered as { result: { bbox_mm: { size: number[] } } }).result.bbox_mm
    expect(bbox.size[0]).toBeCloseTo(46.7, 1)
  })

  it('render waits for the values on screen when a newer change supersedes a render', async () => {
    const bodies: { inputs: { params: Record<string, unknown> }; supersedes?: string }[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/render')) {
        bodies.push((await request.clone().json()) as { inputs: { params: Record<string, unknown> }; supersedes?: string })
      }
    })
    await open()
    await waitFor(() => expect(bodies).toHaveLength(1))

    // One change is submitted, then another lands straight after it: the second submit
    // names the first as the job it supersedes (#241).
    await call('set_param', { name: 'name', value: 'Workshop' })
    await waitFor(() => expect(bodies).toHaveLength(2))
    await call('set_param', { name: 'name', value: 'Nova' })
    await waitFor(() => expect(bodies).toHaveLength(3))
    expect(bodies[2]).toMatchObject({ inputs: { params: { name: 'Nova' } }, supersedes: expect.any(String) })

    // The answer is Nova's render, never the superseded Workshop one (81.4 mm wide).
    const rendered = await call('render', { timeout_ms: 5000 })
    expect(rendered).toMatchObject({ ok: true, result: { status: 'done' } })
    const bbox = (rendered as { result: { bbox_mm: { size: number[] } } }).result.bbox_mm
    expect(bbox.size[0]).toBeCloseTo(46.7, 1)
  })

  it('checks a value against the parameter before anything changes', async () => {
    await open()
    const field = screen.getByRole('textbox', { name: 'Name on the tag' })

    const tooLong = await call('set_param', { name: 'name', value: 'x'.repeat(21) })
    expect(!tooLong.ok && tooLong.error).toMatchObject({ code: 'invalid_args', message: '"name" is at most 20 characters.' })
    const outOfRange = await call('set_param', { name: 'text_size', value: 99 })
    expect(!outOfRange.ok && outOfRange.error.message).toBe('"text_size" is at most 28.')
    const outOfRangeText = await call('set_param', { name: 'text_size', value: '99' })
    expect(!outOfRangeText.ok && outOfRangeText.error.message).toBe('"text_size" is at most 28.')
    const unknown = await call('set_param', { name: 'nope', value: 1 })
    expect(!unknown.ok && unknown.error.code).toBe('invalid_args')

    // set_params is all or nothing.
    const mixed = await call('set_params', { values: { name: 'Ok', text_size: 'big' } })
    expect(!mixed.ok && mixed.error.code).toBe('invalid_args')
    expect(field).toHaveValue('Reagan')
  })

  it('takes a number sent as a numeric string, as the model sometimes sends it (#948)', async () => {
    await open()
    const outcome = await call('set_param', { name: 'text_size', value: '20' })
    expect(outcome).toMatchObject({ ok: true, result: { name: 'text_size', value: 20 } })
  })

  it('shows the tab of the parameter it changed, and resets it', async () => {
    await open()
    expect(screen.getByRole('tab', { name: 'Text' })).toHaveAttribute('aria-selected', 'true')

    expect(await call('set_param', { name: 'keyring_hole', value: false })).toMatchObject({ ok: true })
    expect(screen.getByRole('tab', { name: 'Plate' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('switch', { name: 'Keyring hole' })).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText('1 changed from defaults')).toBeInTheDocument()

    expect(await call('reset_param', { name: 'keyring_hole' })).toMatchObject({ ok: true })
    expect(screen.getByRole('switch', { name: 'Keyring hole' })).toHaveAttribute('aria-checked', 'true')

    await call('set_params', { values: { name: 'Nova', text_size: 20 } })
    expect(screen.getByText('2 changed from defaults')).toBeInTheDocument()
    await call('reset_param', {})
    expect(screen.getByText('Defaults')).toBeInTheDocument()
  })

  it('opens the send dialog but can never confirm the send', async () => {
    const send = vi.spyOn(api, 'sendOutput')
    await open()

    const early = await call('open_print_dialog', { kind: 'send' })
    expect(!early.ok && early.error.message).toMatch(/generate first/)

    const generated = await call('generate', { timeout_ms: 5000 })
    expect(generated).toMatchObject({ ok: true, result: { output: { id: expect.any(String), slug: expect.any(String) }, filed: null } })

    expect(await call('open_print_dialog', { kind: 'send' })).toMatchObject({ ok: true })
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    expect(dialog).toBeInTheDocument()

    const confirm = await call('click', { role: 'button', name: 'Send' })
    expect(!confirm.ok && confirm.error.code).toBe('refused')
    expect(send).not.toHaveBeenCalled()
    expect(bridge.snapshot().dialogs).toEqual(['Send to Bambuddy'])
  })

  /** A request to `path` that waits until the returned function is called, then falls through. */
  function hold(method: 'post', path: string): () => void {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http[method](path, async () => {
        await gate
        return undefined
      }),
    )
    return release
  }

  it('freezes the print dialog\'s picker while a Generate files the project (#665)', async () => {
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json({ projects: projectViews, last_project_id: 1 }),
      ),
    )
    await open()
    // Filed in the remembered project too, which the answer says (#931: the agent records it).
    expect(await call('generate', { timeout_ms: 5000 })).toMatchObject({
      ok: true,
      result: { filed: { project_id: 1, library_file_id: expect.any(Number), created: expect.any(Boolean) } },
    })
    await waitFor(() => expect(screen.getByTestId('print')).toBeEnabled())
    expect(await call('open_print_dialog', { kind: 'print' })).toMatchObject({ ok: true })
    const dialog = await screen.findByRole('dialog')
    // The dialog's project picker is an Advanced step.
    fireEvent.click(await within(dialog).findByRole('switch', { name: 'Advanced' }))
    const dialogPicker = await within(dialog).findByTestId('project-select')
    expect(dialogPicker).toHaveValue('1')
    expect(dialogPicker).toBeEnabled()

    // The dialog is open when the agent generates again: its picker shares the project.
    const release = hold('post', '/api/v1/outputs/:id/project-file')
    const generating = call('generate', { timeout_ms: 5000 })
    await waitFor(() => expect(dialogPicker).toBeDisabled())
    release()
    expect(await generating).toMatchObject({ ok: true })
    // A new output resets the dialog to Simple, so open Advanced again.
    const again = await screen.findByRole('dialog')
    fireEvent.click(await within(again).findByRole('switch', { name: 'Advanced' }))
    await waitFor(() => expect(within(again).getByTestId('project-select')).toBeEnabled())
  })

  it('holds Generate while a project is being created (#665)', async () => {
    const { user } = await open()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    const picker = screen.getByTestId<HTMLSelectElement>('customize-project-select')
    await user.selectOptions(picker, 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')

    const release = hold('post', '/api/v1/print/projects')
    await user.click(screen.getByTestId('create-project'))
    // Its completion switches the project, so neither the button nor the tool may start.
    await waitFor(() => expect(screen.getByTestId('generate')).toBeDisabled())
    const refused = await call('generate', { timeout_ms: 5000 })
    expect(!refused.ok && refused.error.message).toMatch(/still being created/)

    release()
    await waitFor(() => expect(picker.selectedOptions[0]).toHaveTextContent(/Workshop Bins/))
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
  })

  it('holds Print and open_print_dialog while a project is being created (#710 review)', async () => {
    const { user } = await open()
    expect(await call('generate', { timeout_ms: 5000 })).toMatchObject({ ok: true })
    await waitFor(() => expect(screen.getByTestId('print')).toBeEnabled())
    const picker = screen.getByTestId<HTMLSelectElement>('customize-project-select')
    await user.selectOptions(picker, 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')

    const release = hold('post', '/api/v1/print/projects')
    await user.click(screen.getByTestId('create-project'))
    // The dialog's picker would let a reselection be reverted when the create lands.
    await waitFor(() => expect(screen.getByTestId('print')).toBeDisabled())
    const refused = await call('open_print_dialog', { kind: 'print' })
    expect(!refused.ok && refused.error.message).toMatch(/still being created/)

    release()
    await waitFor(() => expect(picker.selectedOptions[0]).toHaveTextContent(/Workshop Bins/))
    await waitFor(() => expect(screen.getByTestId('print')).toBeEnabled())
  })

  it('reports itself in the snapshot and goes unavailable when the page does', async () => {
    const view = await open()
    await call('set_param', { name: 'name', value: 'Nova' })
    expect(bridge.snapshot().page.customize).toMatchObject({
      slug: 'name-keychain',
      changed: [{ name: 'name', value: 'Nova' }],
    })
    view.unmount()
    const gone = await call('get_params')
    expect(!gone.ok && gone.error.code).toBe('unavailable')
  })
})
