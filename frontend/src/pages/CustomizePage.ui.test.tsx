import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ModelSummary } from '../api/types'
import { RENDER_DEBOUNCE_MS } from '../lib/useRenderJob'
import { keychainSchema, models, UI_BROKEN_SLUG, UI_DEMO_SLUG, UI_DEMO_VERSION } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { setUiModuleLoader } from '../template-ui/loadModule'
import type { Host, Mount } from '../template-ui/types'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'

// WebGL does not exist in jsdom: the viewer is a stand-in that renders the page's own
// buttons, which it lays over the scene (as CustomizePage.test.tsx does).
// It counts its mounts, so a test can tell a moved viewer from one that stayed put.
const previews = vi.hoisted(() => ({ mounts: 0 }))
vi.mock('../components/Preview', async () => {
  const { useEffect } = await import('react')
  return {
    Preview: ({ leading, controls }: { leading?: ReactNode; controls?: ReactNode }) => {
      useEffect(() => {
        previews.mounts += 1
      }, [])
      return (
        <div data-testid="preview">
          {leading}
          {controls}
        </div>
      )
    },
  }
})

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

  it('loads the module from the revision the record is at', async () => {
    const urls: string[] = []
    setUiModuleLoader(async (url) => {
      urls.push(url)
      return { mount: demo }
    })
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(shadowText()).toContain('custom'))
    expect(urls).toEqual([`/api/v1/models/${UI_DEMO_SLUG}/versions/${UI_DEMO_VERSION}/ui/index.js`])
  })

  it('loads the live module when the record has no revision', async () => {
    withRecord(UI_DEMO_SLUG, { version: null })
    const urls: string[] = []
    setUiModuleLoader(async (url) => {
      urls.push(url)
      return { mount: demo }
    })
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(shadowText()).toContain('custom'))
    expect(urls).toEqual([`/api/v1/models/${UI_DEMO_SLUG}/ui/index.js`])
  })

  it('keeps a UI-state set made just before a parameter edit', async () => {
    let host: Host | undefined
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot, given: Host) => {
        host = given
        const param = document.createElement('sb-param')
        param.setAttribute('name', 'name')
        root.append(param)
      },
    }))
    open(UI_DEMO_SLUG)
    const field = await waitFor(() => {
      const found = shadow()?.querySelector('input')
      if (!found) throw new Error('no field yet')
      return found
    })
    // No re-render between the two: the field's write must not start from the old inputs.
    host?.inputs.set({ demo: { touched: true } })
    fireEvent.change(field, { target: { value: 'Zed' } })
    await waitFor(() => expect((host?.inputs.get()['params'] as Record<string, unknown>)['name']).toBe('Zed'))
    expect(host?.inputs.get()['demo']).toEqual({ touched: true })
  })

  it('keeps both of two writes in one tick, and get() sees them at once (#864)', async () => {
    let host: Host | undefined
    setUiModuleLoader(async () => ({
      mount: (_root: ShadowRoot, given: Host) => {
        host = given
      },
    }))
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(host).toBeDefined())
    host?.inputs.set({ params: { name: 'Seven' } })
    host?.inputs.set({ history: [7] })
    // Synchronously, before React commits either write.
    const now = host?.inputs.get()
    expect((now?.['params'] as Record<string, unknown>)['name']).toBe('Seven')
    expect(now?.['history']).toEqual([7])
    await waitFor(() => expect((host?.inputs.get()['params'] as Record<string, unknown>)['name']).toBe('Seven'))
    expect(host?.inputs.get()['history']).toEqual([7])
  })

  it('a write right after a preset load starts from the preset', async () => {
    let host: Host | undefined
    setUiModuleLoader(async () => ({
      mount: (_root: ShadowRoot, given: Host) => {
        host = given
      },
    }))
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(host).toBeDefined())
    const initial = (host?.inputs.get()['params'] as Record<string, unknown>)['name']
    const saved = await host!.presets.save('at defaults')
    host?.inputs.set({ params: { name: 'Changed' } })
    await host!.presets.load(saved.id)
    // No render between the load and the write: the write must start from the preset.
    host?.inputs.set({ picked: 'x' })
    await waitFor(() => expect(host?.inputs.get()['picked']).toBe('x'))
    expect((host?.inputs.get()['params'] as Record<string, unknown>)['name']).toBe(initial)
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

  it('drops "Saved …" once a UI-state-only change leaves the output behind (#848)', async () => {
    let host: Host | undefined
    setUiModuleLoader(async () => ({
      mount: (_root: ShadowRoot, given: Host) => {
        host = given
      },
    }))
    const { user } = open(UI_DEMO_SLUG)
    await waitFor(() => expect(screen.getByTestId('generate')).toBeEnabled(), { timeout: 5000 })
    await user.click(screen.getByTestId('generate'))
    await waitFor(() => expect(screen.getByText(/^Saved /)).toBeInTheDocument())
    host?.inputs.set({ ...host.inputs.get(), demo: { touched: true } })
    await waitFor(() => expect(screen.queryByText(/^Saved /)).toBeNull())
    expect(screen.getByTestId('generate')).toBeEnabled()
  }, 20_000)

  it('a colour bound outside params starts no render and leaves the extruder numbers to params', async () => {
    let host: Host | undefined
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot, given: Host) => {
        host = given
        const param = document.createElement('sb-param')
        param.setAttribute('name', 'text_color')
        param.setAttribute('bind', 'style.c')
        root.append(param)
      },
    }))
    let renders = 0
    server.use(
      http.post('/api/v1/models/:slug/render', () => {
        renders += 1
      }),
    )
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(renders).toBe(1))
    const before = structuredClone(host?.inputs.get()['params'])
    const text = await waitFor(() => {
      const found = shadow().querySelector<HTMLInputElement>('input[type="text"]')
      if (!found) throw new Error('no colour field yet')
      return found
    })
    const extruder = () => shadow().textContent?.match(/extruder (\d+)/)?.[1]
    const numbered = extruder()
    fireEvent.change(text, { target: { value: '#123456' } })
    await waitFor(() => expect(host?.inputs.get()['style']).toEqual({ c: '#123456' }))
    await new Promise((resolve) => setTimeout(resolve, RENDER_DEBOUNCE_MS * 2))
    expect(renders).toBe(1)
    expect(host?.inputs.get()['params']).toEqual(before)
    expect(extruder()).toBe(numbered)
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

  it("opens a revision with that revision's ui declaration, not the current one", async () => {
    const imports: string[] = []
    setUiModuleLoader(async (url) => {
      imports.push(url)
      return { mount: demo }
    })
    const old = 'b'.repeat(40)
    // The current record declares a UI; the revision from before it declares none.
    server.use(
      http.get('/api/v1/models/:slug/versions/:commit/schema', () =>
        HttpResponse.json({ ...keychainSchema, ui: null, ui_error: null }),
      ),
    )
    renderPage(<CustomizePage />, { route: `/m/${UI_DEMO_SLUG}?version=${old}`, path: '/m/:slug' })
    await waitFor(() => expect(document.querySelector('[data-param]')).not.toBeNull())
    expect(document.querySelector('[data-testid="template-ui"]')).toBeNull()
    expect(screen.queryByRole('alert', { name: /template interface/i })).toBeNull()
    expect(imports).toEqual([])
  })

  it('tries the interface again from the banner after a transient failure', async () => {
    let imports = 0
    setUiModuleLoader(async () => {
      imports += 1
      if (imports === 1) throw new TypeError('Failed to fetch dynamically imported module')
      return { mount: demo }
    })
    const { user } = open(UI_DEMO_SLUG)
    const banner = await screen.findByRole('alert', { name: /template interface/i }, { timeout: 5000 })
    await user.click(within(banner).getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(shadowText()).toMatch(/^custom /), { timeout: 5000 })
    expect(screen.queryByRole('alert', { name: /template interface/i })).toBeNull()
  })

  it('makes one output from two host.generate() calls at once', async () => {
    let posts = 0
    server.use(
      http.post('/api/v1/models/:slug/outputs', () => {
        posts += 1
        return undefined // on to the default handler
      }),
    )
    let host: Host | undefined
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot, given: Host) => {
        host = given
        demo(root, given, { slot: 'panel', version: null, theme: 'light', api: 1 })
      },
    }))
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(host).toBeDefined(), { timeout: 5000 })
    const first = host!.generate()
    const second = host!.generate()
    expect(second).toBe(first)
    const [a, b] = await Promise.all([first, second])
    expect(b).toEqual(a)
    expect(posts).toBe(1)
  }, 20_000)

  it('reports host.openPrint for an output not on screen in the banner, never as a throw', async () => {
    let host: Host | undefined
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot, given: Host) => {
        host = given
        demo(root, given, { slot: 'panel', version: null, theme: 'light', api: 1 })
      },
    }))
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(host).toBeDefined(), { timeout: 5000 })
    expect(() => host!.openPrint('not-on-screen')).not.toThrow()
    const banner = await screen.findByRole('alert', { name: /template interface/i }, { timeout: 5000 })
    expect(banner).toHaveTextContent('openPrint(not-on-screen): output is not the one on screen; call generate() first')
  })

  it('tries the interface again on another revision after one failed', async () => {
    setUiModuleLoader(async (url) => {
      if (url.includes(`/versions/${'a'.repeat(40)}/`)) return { mount: () => { throw new Error('old revision broke') } }
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

  it("mounts a page-slot template's preview once when the schema lands before the record", async () => {
    const model = models.find((m) => m.slug === UI_DEMO_SLUG)
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    let schemaServed = false
    server.use(
      http.get('/api/v1/models/:slug/schema', () => {
        schemaServed = true
        return HttpResponse.json(keychainSchema)
      }),
      http.get('/api/v1/models/:slug', async () => {
        await held
        return HttpResponse.json({ ...model, ui: { module: 'ui/index.js', slot: 'page', api: 1 } })
      }),
    )
    setUiModuleLoader(async () => ({
      mount: (root: ShadowRoot) => {
        root.innerHTML = '<sb-preview></sb-preview>'
      },
    }))
    // The viewer is lazy: load it first, so "no preview yet" is the page's choice.
    await import('../components/Preview')
    previews.mounts = 0
    open(UI_DEMO_SLUG)
    await waitFor(() => expect(schemaServed).toBe(true))
    await new Promise((resolve) => setTimeout(resolve, 300))
    expect(screen.queryByText('Loading the viewer')).toBeNull()
    expect(screen.queryByTestId('preview')).toBeNull()
    expect(previews.mounts).toBe(0)
    release()
    await waitFor(() => expect(shadow().querySelector('[data-testid="preview"]')).not.toBeNull(), { timeout: 5000 })
    expect(previews.mounts).toBe(1)
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
