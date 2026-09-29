import { fireEvent, screen, waitFor } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ModelSummary } from '../api/types'
import { RENDER_DEBOUNCE_MS } from '../lib/useRenderJob'
import { keychainSchema, models, UI_BROKEN_SLUG, UI_DEMO_SLUG } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { setUiModuleLoader } from '../template-ui/loadModule'
import type { Host, Mount } from '../template-ui/types'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'

// WebGL does not exist in jsdom: the viewer is a stand-in that renders the page's own
// buttons, which it lays over the scene (as CustomizePage.test.tsx does).
vi.mock('../components/Preview', () => ({
  Preview: ({ leading, controls }: { leading?: ReactNode; controls?: ReactNode }) => (
    <div data-testid="preview">
      {leading}
      {controls}
    </div>
  ),
}))

/** The record `GET /models/{slug}` answers for ``slug``, changed by ``patch``. */
function withRecord(slug: string, patch: Partial<ModelSummary>) {
  const model = models.find((m) => m.slug === slug)
  if (!model) throw new Error(`no fixture ${slug}`)
  server.use(http.get('/api/v1/models/:slug', ({ params }) =>
    params['slug'] === slug ? HttpResponse.json({ ...model, ...patch }) : undefined,
  ))
}

function shadow(): ShadowRoot {
  const root = document.querySelector('[data-testid="template-ui"]')?.shadowRoot
  if (!root) throw new Error('no template UI')
  return root
}

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

  it('shows the banner for a ui declaration model.json could not hold (ui null, ui_error)', async () => {
    withRecord(UI_DEMO_SLUG, { ui: null, ui_error: 'ui.module must name a file under ui/' })
    open(UI_DEMO_SLUG)
    const banner = await screen.findByRole('alert', { name: /template interface/i }, { timeout: 5000 })
    expect(banner).toHaveTextContent('model.json')
    expect(banner).toHaveTextContent('ui.module must name a file under ui/')
    await waitFor(() => expect(document.querySelector('[data-param]')).not.toBeNull())
    expect(document.querySelector('[data-testid="template-ui"]')).toBeNull()
  })

  it('falls back with a banner when the module cannot be imported', async () => {
    setUiModuleLoader(async () => {
      throw new TypeError('Failed to fetch dynamically imported module')
    })
    open(UI_DEMO_SLUG)
    const banner = await screen.findByRole('alert', { name: /template interface/i }, { timeout: 5000 })
    expect(banner).toHaveTextContent('Failed to fetch dynamically imported module')
    await waitFor(() => expect(document.querySelector('[data-param]')).not.toBeNull())
  })

  it('falls back with a banner for a host API this page does not speak', async () => {
    withRecord(UI_DEMO_SLUG, { ui: { module: 'ui/index.js', slot: 'panel', api: 2 } })
    open(UI_DEMO_SLUG)
    const banner = await screen.findByRole('alert', { name: /template interface/i }, { timeout: 5000 })
    expect(banner).toHaveTextContent('ui/index.js')
    await waitFor(() => expect(document.querySelector('[data-param]')).not.toBeNull())
  })

  it('labels an import whose origin_url is not a URL as imported, and still opens', async () => {
    withRecord(UI_DEMO_SLUG, { origin_url: 'not a url' })
    open(UI_DEMO_SLUG)
    expect(await screen.findByTestId('ui-origin', {}, { timeout: 5000 })).toHaveTextContent(
      'Custom interface · imported',
    )
  })

  it('tries the interface again on another revision after one failed', async () => {
    setUiModuleLoader(async (url) => {
      if (url.includes('/versions/')) return { mount: () => { throw new Error('old revision broke') } }
      return { mount: demo }
    })
    const { user } = renderPage(<CustomizePage />, {
      route: `/m/${UI_DEMO_SLUG}?version=${'a'.repeat(40)}`,
      path: '/m/:slug',
    })
    await screen.findByRole('alert', { name: /template interface/i }, { timeout: 5000 })
    await user.click(screen.getByRole('button', { name: 'Back to current' }))
    await waitFor(() => expect(shadowText()).toMatch(/^custom /), { timeout: 5000 })
    expect(screen.queryByRole('alert', { name: /template interface/i })).toBeNull()
  })
})

describe('the page slot', () => {
  beforeEach(() => {
    withRecord(UI_DEMO_SLUG, { ui: { module: 'ui/index.js', slot: 'page', api: 1 } })
  })
  afterEach(() => {
    Reflect.deleteProperty(document, 'fullscreenEnabled')
  })

  it('covers the frame in full screen where the Fullscreen API is refused, with no flyout button', async () => {
    Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: false })
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot) => {
        root.innerHTML = '<sb-preview></sb-preview><sb-generate></sb-generate>'
      },
    }))
    open(UI_DEMO_SLUG)
    const enter = await waitFor(
      () => {
        const button = shadow().querySelector<HTMLButtonElement>('button[aria-label="Full screen"]')
        if (!button) throw new Error('no Full screen button yet')
        return button
      },
      { timeout: 5000 },
    )
    const workspace = screen.getByTestId('workspace')
    expect(workspace.className).not.toContain('fixed')
    fireEvent.click(enter)
    await waitFor(() => expect(workspace.className).toContain('fixed inset-0'))
    expect(shadow().querySelector('button[aria-label="Exit full screen"]')).not.toBeNull()
    // The page slot has no parameters flyout to open.
    expect(shadow().querySelector('button[aria-label="Parameters"]')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Parameters' })).toBeNull()
  })

  it("reports the template Generate's error, and takes one click at a time", async () => {
    let posts = 0
    server.use(
      http.post('/api/v1/models/:slug/outputs', async () => {
        posts += 1
        await delay(300)
        return HttpResponse.json({ type: 'about:blank', title: 'Server error', status: 500, detail: 'disk full' }, { status: 500 })
      }),
    )
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot) => {
        root.innerHTML = '<sb-generate></sb-generate>'
      },
    }))
    open(UI_DEMO_SLUG)
    const generate = await waitFor(
      () => {
        const button = shadow().querySelector<HTMLButtonElement>('sb-generate button')
        if (!button || button.disabled || button.textContent !== 'Generate') throw new Error('not ready')
        return button
      },
      { timeout: 8000 },
    )
    fireEvent.click(generate)
    await waitFor(() => expect(generate).toBeDisabled())
    fireEvent.click(generate)
    await waitFor(() => expect(shadow().textContent).toContain('disk full'), { timeout: 5000 })
    expect(posts).toBe(1)
    expect(generate).not.toBeDisabled()
  })
})
