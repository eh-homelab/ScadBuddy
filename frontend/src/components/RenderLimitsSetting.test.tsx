import { screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { mockRenderLimitWrites } from '../mocks/features/renderLimits'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { RenderLimitsSetting } from './RenderLimitsSetting'

describe('RenderLimitsSetting', () => {
  it('shows the defaults, and saves new limits with an override', async () => {
    const { user } = renderPage(<RenderLimitsSetting />)
    const perCaller = await screen.findByRole('spinbutton', { name: 'Renders at once, per caller' })
    const global = screen.getByRole('spinbutton', { name: 'Renders at once, in all' })
    const window = screen.getByRole('spinbutton', { name: 'Renders per 10 min, per caller' })
    expect(perCaller).toHaveValue(4)
    expect(global).toHaveValue(0)
    expect(window).toHaveValue(0)
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    await user.clear(perCaller)
    await user.type(perCaller, '8')
    await user.clear(global)
    await user.type(global, '12')
    await user.click(screen.getByRole('button', { name: 'Add override' }))
    const overrides = screen.getByRole('group', { name: 'Per-caller overrides' })
    await user.type(within(overrides).getByRole('combobox', { name: 'Caller' }), 'browser')
    const atOnce = within(overrides).getByRole('spinbutton', { name: 'At once' })
    await user.clear(atOnce)
    await user.type(atOnce, '16')
    await user.click(save)

    expect(await screen.findByRole('status')).toHaveTextContent('Saved. The assistant uses these limits now.')
    expect(mockRenderLimitWrites()).toEqual([
      { per_principal: 8, global: 12, per_window: 0, overrides: [{ principal: 'browser', concurrent: 16 }] },
    ])
    expect(save).toBeDisabled()
  })

  it('removes an override', async () => {
    server.use(
      http.get('/api/v1/ai/settings/render-limits', () =>
        HttpResponse.json({ per_principal: 4, global: 0, per_window: 0, overrides: [{ principal: 'token:x', concurrent: 9 }] }),
      ),
    )
    const { user } = renderPage(<RenderLimitsSetting />)
    await user.click(await screen.findByRole('button', { name: 'Remove the override for token:x' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByRole('status')
    expect(mockRenderLimitWrites().at(-1)?.overrides).toEqual([])
  })

  it('is for the user only: an agent cannot raise its own limits', async () => {
    renderPage(<RenderLimitsSetting />)
    expect(await screen.findByRole('button', { name: 'Save' })).toHaveAttribute('data-agent-user-only')
  })

  it('shows why the agent refused a value', async () => {
    server.use(
      http.put('/api/v1/ai/settings/render-limits', () =>
        HttpResponse.json({ detail: 'global: Too big: expected number to be <=256' }, { status: 400 }),
      ),
    )
    const { user } = renderPage(<RenderLimitsSetting />)
    const global = await screen.findByRole('spinbutton', { name: 'Renders at once, in all' })
    await user.clear(global)
    await user.type(global, '5')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('global: Too big')
  })

  it('is hidden when the agent service cannot answer', async () => {
    server.use(http.get('/api/v1/ai/settings/render-limits', () => HttpResponse.json({ detail: 'no db' }, { status: 503 })))
    const { container } = renderPage(<RenderLimitsSetting />)
    await new Promise((r) => setTimeout(r, 50))
    expect(container.querySelector('section')).toBeNull()
  })
})
