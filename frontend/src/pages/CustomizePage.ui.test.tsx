import { screen, waitFor } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { RENDER_DEBOUNCE_MS } from '../lib/useRenderJob'
import { keychainSchema, UI_BROKEN_SLUG, UI_DEMO_SLUG } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { setUiModuleLoader } from '../template-ui/loadModule'
import type { Host, Mount } from '../template-ui/types'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'

const demo: Mount = (root, host) => {
  const p = document.createElement('p')
  p.textContent = `custom ${String((host.inputs.get()['params'] as Record<string, unknown>)['name'])}`
  root.append(p)
}

beforeEach(() => {
  setUiModuleLoader(async (url) => {
    if (url.includes(`/models/${UI_BROKEN_SLUG}/`)) return { mount: () => { throw new Error('broken on purpose') } }
    return { mount: demo }
  })
})
afterEach(() => setUiModuleLoader(null))

function open(slug: string) {
  return renderPage(<CustomizePage />, { route: `/m/${slug}`, path: '/m/:slug' })
}

function shadowText(): string {
  return document.querySelector('[data-testid="template-ui"]')?.shadowRoot?.textContent ?? ''
}

describe('CustomizePage with a template UI', () => {
  it('mounts the template UI in place of the generated form, and says where it came from', async () => {
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(shadowText()).toMatch(/^custom /))
    expect(screen.getByTestId('ui-origin')).toHaveTextContent('Custom interface · mine')
    expect(screen.queryByRole('tablist')).toBeNull()
  })

  it('falls back to the generated form with a banner naming the file and the error', async () => {
    open(UI_BROKEN_SLUG)
    const banner = await screen.findByRole('alert', { name: /template interface/i }, { timeout: 5000 })
    expect(banner).toHaveTextContent('ui/index.js')
    expect(banner).toHaveTextContent('broken on purpose')
    expect(await screen.findByTestId('generate')).toBeInTheDocument()
    expect(document.querySelector('[data-param]')).not.toBeNull()
  })

  it('mounts only once a delayed schema is there, and host.schema() answers', async () => {
    server.use(
      http.get('/api/v1/models/:slug/schema', async () => {
        await delay(300)
        return HttpResponse.json(keychainSchema)
      }),
    )
    let seen: unknown
    setUiModuleLoader(async () => ({
      mount: async (_root: ShadowRoot, host: Host) => {
        seen = await host.schema()
      },
    }))
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(seen).toEqual(keychainSchema), { timeout: 3000 })
    expect(screen.queryByRole('alert', { name: /template interface/i })).toBeNull()
  })

  it('a UI-state-only set starts no new render', async () => {
    let host: Host | undefined
    setUiModuleLoader(async () => ({
      mount: (_root: ShadowRoot, given: Host) => {
        host = given
      },
    }))
    let renders = 0
    server.use(
      // Counts, then falls through to the regular mock handler (msw v2: no return value).
      http.post('/api/v1/models/:slug/render', () => {
        renders += 1
      }),
    )
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(renders).toBe(1))
    host?.inputs.set({ demo: { touched: true } })
    await new Promise((resolve) => setTimeout(resolve, RENDER_DEBOUNCE_MS * 2))
    expect(renders).toBe(1)
    expect(host?.inputs.get()['demo']).toEqual({ touched: true })
  })

  it('keeps a template without ui exactly on the generated form', async () => {
    open('name-keychain')
    await waitFor(() => expect(document.querySelector('[data-param]')).not.toBeNull())
    expect(document.querySelector('[data-testid="template-ui"]')).toBeNull()
    expect(screen.queryByTestId('ui-origin')).toBeNull()
  })
})
