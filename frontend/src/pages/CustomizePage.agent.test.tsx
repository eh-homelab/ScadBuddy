import { screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { bridge } from '../agent/bridge'
import { useGlobalAgentTools } from '../agent/global'
import { api } from '../api/client'
import type { Job } from '../api/types'
import { RENDER_DEBOUNCE_MS } from '../lib/useRenderJob'
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
      bodies.push(((await request.clone().json()) as { params: Record<string, unknown> }).params)
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

  it('checks a value against the parameter before anything changes', async () => {
    await open()
    const field = screen.getByRole('textbox', { name: 'Name on the tag' })

    const tooLong = await call('set_param', { name: 'name', value: 'x'.repeat(21) })
    expect(!tooLong.ok && tooLong.error).toMatchObject({ code: 'invalid_args', message: '"name" is at most 20 characters.' })
    const outOfRange = await call('set_param', { name: 'text_size', value: 99 })
    expect(!outOfRange.ok && outOfRange.error.message).toBe('"text_size" is at most 28.')
    const unknown = await call('set_param', { name: 'nope', value: 1 })
    expect(!unknown.ok && unknown.error.code).toBe('invalid_args')

    // set_params is all or nothing.
    const mixed = await call('set_params', { values: { name: 'Ok', text_size: 'big' } })
    expect(!mixed.ok && mixed.error.code).toBe('invalid_args')
    expect(field).toHaveValue('Reagan')
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
    expect(generated).toMatchObject({ ok: true, result: { output: { id: expect.any(String) } } })

    expect(await call('open_print_dialog', { kind: 'send' })).toMatchObject({ ok: true })
    const dialog = await screen.findByRole('dialog', { name: 'Send to Bambuddy' })
    expect(dialog).toBeInTheDocument()

    const confirm = await call('click', { role: 'button', name: 'Send' })
    expect(!confirm.ok && confirm.error.code).toBe('refused')
    expect(send).not.toHaveBeenCalled()
    expect(bridge.snapshot().dialogs).toEqual(['Send to Bambuddy'])
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
