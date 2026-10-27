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
import { setMockRemembered } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'
import { chooseProject, pickerValue } from '../test/projectPicker'

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
  // The first render goes through the whole mocked pipeline; on a loaded host it took
  // over 4 s to show (#1485).
  await waitFor(() => expect(screen.getByTestId('bbox')).toBeInTheDocument(), { timeout: 10_000 })
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
    setMockRemembered({ modelProjects: { 'name-keychain': { project_id: 1 } } })
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
    expect(pickerValue(dialogPicker)).toBe('1')
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

  it('refuses to file away from the remembered project until the agent names one (#1660)', async () => {
    server.use(
      http.get('/api/v1/print/projects', () =>
        HttpResponse.json({ projects: projectViews, last_project_id: 1 }),
      ),
    )
    setMockRemembered({ modelProjects: { 'name-keychain': { project_id: 2 } } })
    const { user } = await open()
    const picker = screen.getByTestId('customize-project-select')
    await waitFor(() => expect(pickerValue(picker)).toBe('2'))
    await chooseProject(user, picker, 1)

    const refused = await call('generate', { timeout_ms: 5000 })
    expect(!refused.ok && refused.error.message).toMatch(
      /last filed in project 2 \(Gridfinity Bins\).*picker has project 1 \(Reagan Keychain\)/,
    )
    // Named, it files there and the picker follows.
    expect(await call('generate', { timeout_ms: 5000, project_id: 2 })).toMatchObject({
      ok: true,
      result: { filed: { project_id: 2 } },
    })
    expect(pickerValue(picker)).toBe('2')
  })

  it('files nothing with project_id null, past the remembered-project check (#1660)', async () => {
    setMockRemembered({ modelProjects: { 'name-keychain': { project_id: 2 } } })
    await open()
    const picker = screen.getByTestId('customize-project-select')
    await waitFor(() => expect(pickerValue(picker)).toBe('2'))

    const generated = await call('generate', { timeout_ms: 5000, project_id: null })
    expect(generated).toMatchObject({ ok: true, result: { filed: null } })
    expect(pickerValue(picker)).toBe('')
  })

  it('refuses a project_id Bambuddy does not list, saving nothing (#1660)', async () => {
    const saved = vi.fn()
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/outputs')) saved()
    })
    await open()
    // The picker shows once the project list has loaded.
    const picker = await screen.findByTestId('customize-project-select')

    const refused = await call('generate', { timeout_ms: 5000, project_id: 999 })
    expect(refused).toMatchObject({ ok: false, error: { code: 'invalid_args' } })
    expect(!refused.ok && refused.error.message).toMatch(/no project 999/)
    expect(saved).not.toHaveBeenCalled()
    expect(pickerValue(picker)).toBe('')
  })

  it('holds Generate while a project is being created (#665)', async () => {
    const { user } = await open()
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
    const picker = screen.getByTestId('customize-project-select')
    await chooseProject(user, picker, 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')

    const release = hold('post', '/api/v1/print/projects')
    await user.click(screen.getByTestId('create-project'))
    // Its completion switches the project, so neither the button nor the tool may start.
    await waitFor(() => expect(screen.getByTestId('generate')).toBeDisabled())
    const refused = await call('generate', { timeout_ms: 5000 })
    expect(!refused.ok && refused.error.message).toMatch(/still being created/)

    release()
    await waitFor(() => expect(picker).toHaveTextContent(/Workshop Bins/))
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled())
  })

  it('holds Print and open_print_dialog while a project is being created (#710 review)', async () => {
    const { user } = await open()
    expect(await call('generate', { timeout_ms: 5000 })).toMatchObject({ ok: true })
    await waitFor(() => expect(screen.getByTestId('print')).toBeEnabled())
    const picker = screen.getByTestId('customize-project-select')
    await chooseProject(user, picker, 'new')
    await user.type(screen.getByTestId('new-project-name'), 'Workshop Bins')

    const release = hold('post', '/api/v1/print/projects')
    await user.click(screen.getByTestId('create-project'))
    // The dialog's picker would let a reselection be reverted when the create lands.
    await waitFor(() => expect(screen.getByTestId('print')).toBeDisabled())
    const refused = await call('open_print_dialog', { kind: 'print' })
    expect(!refused.ok && refused.error.message).toMatch(/still being created/)

    release()
    await waitFor(() => expect(picker).toHaveTextContent(/Workshop Bins/))
    await waitFor(() => expect(screen.getByTestId('print')).toBeEnabled())
  })

  /**
   * Holds `slug`'s schema GET until the returned function is called, then answers with
   * `respond` (or falls through to the mock). Other slugs load as usual.
   */
  function holdSchema(slug: string, respond?: () => Response): () => void {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.get('/api/v1/models/:slug/schema', async ({ params }) => {
        if (params['slug'] !== slug) return undefined
        await gate
        return respond?.()
      }),
    )
    return release
  }

  /** Opens the page with its schema still loading: the tools are live, `loaded` is false. */
  async function openLoading(route = '/m/name-keychain') {
    renderPage(
      <Shell>
        <CustomizePage />
      </Shell>,
      { route, path: '/m/:slug' },
    )
    await waitFor(() => expect(bridge.snapshot().page.customize).toMatchObject({ loaded: false }))
  }

  it('waits for a schema still loading instead of answering "still loading" (#4885)', async () => {
    const release = holdSchema('name-keychain')
    await openLoading()

    const rendered = call('render', { timeout_ms: 10_000 })
    const set = call('set_param', { name: 'name', value: 'Nova' })
    await new Promise((resolve) => setTimeout(resolve, 100))
    release()

    expect(await set).toMatchObject({ ok: true, result: { name: 'name', value: 'Nova' } })
    expect(await rendered).toMatchObject({ ok: true, result: { status: 'done' } })
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Nova')
  })

  it('answers unavailable, not for another model, when the page moves while a call waits (#4894)', async () => {
    const release = holdSchema('name-keychain')
    try {
      await openLoading()

      const params = call('get_params')
      const rendered = call('render', { timeout_ms: 10_000 })
      await new Promise((resolve) => setTimeout(resolve, 100))
      expect(await call('navigate', { route: '/m/gridfinity-bin' })).toMatchObject({ ok: true })
      await waitFor(() => expect(bridge.snapshot().page.customize).toMatchObject({ slug: 'gridfinity-bin', loaded: true }))

      expect(await params).toMatchObject({ ok: false, error: { code: 'unavailable' } })
      expect(await rendered).toMatchObject({ ok: false, error: { code: 'unavailable' } })
    } finally {
      release()
    }
  })

  it('render answers unavailable when the page moves while it waits for the preview (#4900)', async () => {
    // The schema loads; name-keychain's first render is held, so `render` is past the
    // schema wait and waiting for the preview when the page moves.
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    let held = 0
    let passed = 0
    server.use(
      http.post('/api/v1/models/:slug/render', async ({ params }) => {
        if (params['slug'] !== 'name-keychain') return undefined
        held += 1
        await gate
        passed += 1
        return undefined
      }),
    )
    try {
      renderPage(
        <Shell>
          <CustomizePage />
        </Shell>,
        { route: '/m/name-keychain', path: '/m/:slug' },
      )
      await waitFor(() => expect(bridge.snapshot().page.customize).toMatchObject({ slug: 'name-keychain', loaded: true }))

      // The render is held in flight, so `render` below waits for the preview, not the schema.
      await waitFor(() => expect(held).toBe(1))

      const rendered = call('render', { timeout_ms: 10_000 })
      await new Promise((resolve) => setTimeout(resolve, 100))
      expect(await call('navigate', { route: '/m/gridfinity-bin' })).toMatchObject({ ok: true })

      expect(await rendered).toMatchObject({ ok: false, error: { code: 'unavailable' } })
    } finally {
      release()
    }
    // One preview render of name-keychain, and no second one hiding behind the first: once
    // every held request has gone through and gridfinity-bin has rendered, the page is
    // settled, so a render the release set off has had its chance to start.
    await waitFor(() => {
      // `passed === held` is what orders this check after the release: until the held
      // request has gone through, the render it unblocks has not had its chance.
      expect(passed).toBe(held)
      expect(bridge.snapshot().page.customize).toMatchObject({ slug: 'gridfinity-bin', render: 'done' })
    })
    expect(held).toBe(1)
  })

  it('answers at once when the schema fails to load while a call waits (#4894)', async () => {
    const release = holdSchema('name-keychain', () =>
      HttpResponse.json({ detail: 'the schema service fell over' }, { status: 500 }),
    )
    await openLoading()

    const params = call('get_params')
    await new Promise((resolve) => setTimeout(resolve, 100))
    const released = performance.now()
    release()

    const outcome = await params
    expect(outcome).toMatchObject({ ok: false, error: { code: 'timeout', message: expect.stringMatching(/^The model did not load: /) } })
    // Well inside the 10 s wait: the failure ends it.
    expect(performance.now() - released).toBeLessThan(2000)
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
