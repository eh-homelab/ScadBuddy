import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import type { HostDeps } from './host'
import { setUiModuleLoader } from './loadModule'
import { TemplateUi } from './TemplateUi'
import type { Host, Mount } from './types'

const UI = { module: 'ui/index.js', slot: 'panel' as const, api: 1 }

function deps(): HostDeps {
  return {
    slug: 'name-keychain',
    version: 'abc1234',
    getSchema: () => keychainSchema,
    getInputs: () => ({ params: { name: 'Hi' } }),
    setInputs: vi.fn(),
    generate: vi.fn(),
    openPrint: vi.fn(),
    presets: { list: vi.fn(), save: vi.fn(), load: vi.fn() },
    onDescribe: vi.fn(),
  }
}

function withModule(mount: Mount | undefined) {
  const urls: string[] = []
  setUiModuleLoader(async (url) => {
    urls.push(url)
    return mount ? { mount } : {}
  })
  return urls
}

function shadow(container: HTMLElement): ShadowRoot {
  const root = container.querySelector('[data-testid="template-ui"]')?.shadowRoot
  if (!root) throw new Error('no shadow root')
  return root
}

afterEach(() => setUiModuleLoader(null))

describe('TemplateUi', () => {
  it('loads the module from the pinned revision and mounts it into a shadow root', async () => {
    const urls = withModule((root, host, ctx) => {
      const p = document.createElement('p')
      p.textContent = `${ctx.slot} ${ctx.version} api${host.api} ${String(host.inputs.get()['params'] && 'inputs')}`
      root.append(p)
    })
    const { container } = render(
      <TemplateUi slug="name-keychain" ui={UI} version="abc1234" deps={deps()} inputs={{ params: {} }} onFailure={vi.fn()} />,
    )
    await waitFor(() => expect(shadow(container).textContent).toBe('panel abc1234 api1 inputs'))
    expect(urls).toEqual(['/api/v1/models/name-keychain/versions/abc1234/ui/index.js'])
  })

  it.each([
    ['mount throws', (() => { throw new Error('boom') }) as Mount, 'boom'],
    ['async mount rejects', (async () => { throw new Error('later') }) as Mount, 'later'],
    ['no mount export', undefined, 'does not export a mount function'],
  ])('reports a failure when %s', async (_name, mount, message) => {
    withModule(mount)
    const onFailure = vi.fn()
    render(<TemplateUi slug="name-keychain" ui={UI} version={undefined} deps={deps()} inputs={{ params: {} }} onFailure={onFailure} />)
    await waitFor(() => expect(onFailure).toHaveBeenCalledWith({ file: 'ui/index.js', message: expect.stringContaining(message) }))
  })

  it('refuses an unsupported api major without loading anything', async () => {
    const urls = withModule(vi.fn())
    const onFailure = vi.fn()
    render(<TemplateUi slug="s" ui={{ ...UI, api: 7 }} version={undefined} deps={deps()} inputs={{ params: {} }} onFailure={onFailure} />)
    await waitFor(() => expect(onFailure).toHaveBeenCalledWith({ file: 'ui/index.js', message: expect.stringMatching(/API 7.*supports 1/) }))
    expect(urls).toEqual([])
  })

  it('runs the cleanup and empties the root on unmount, and notifies input changes', async () => {
    const cleanup = vi.fn()
    const seen: unknown[] = []
    withModule((root, host) => {
      root.append(document.createElement('span'))
      host.inputs.subscribe((inputs) => seen.push(inputs))
      return cleanup
    })
    const props = { slug: 's', ui: UI, version: undefined, deps: deps(), onFailure: vi.fn() }
    const { container, rerender, unmount } = render(<TemplateUi {...props} inputs={{ params: { a: 1 } }} />)
    await waitFor(() => expect(shadow(container).childNodes.length).toBe(1))
    rerender(<TemplateUi {...props} inputs={{ params: { a: 2 } }} />)
    await waitFor(() => expect(seen).toContainEqual({ params: { a: 2 } }))
    const root = shadow(container)
    unmount()
    expect(cleanup).toHaveBeenCalledOnce()
    expect(root.childNodes.length).toBe(0)
  })

  it('gives a host kept after a template switch nothing of the next template', async () => {
    let kept: Host | undefined
    withModule((root, host) => {
      kept ??= host
      root.append(document.createElement('span'))
    })
    const a = deps()
    const b = { ...deps(), slug: 'other', getInputs: () => ({ params: { name: 'B' } }) }
    const { container, rerender } = render(
      <TemplateUi slug="name-keychain" ui={UI} version={undefined} deps={a} inputs={{ params: {} }} onFailure={vi.fn()} />,
    )
    await waitFor(() => expect(kept).toBeDefined())
    rerender(<TemplateUi slug="other" ui={UI} version={undefined} deps={b} inputs={{ params: {} }} onFailure={vi.fn()} />)
    await waitFor(() => expect(shadow(container).childNodes.length).toBe(1))
    const old = kept as Host
    expect(() => old.inputs.get()).toThrow(/unmounted/)
    await expect(old.schema()).rejects.toThrow(/unmounted/)
    await expect(old.presets.list()).rejects.toThrow(/unmounted/)
    await expect(old.presets.save('x')).rejects.toThrow(/unmounted/)
    await expect(old.presets.load('x')).rejects.toThrow(/unmounted/)
    expect(b.presets.list).not.toHaveBeenCalled()
    expect(b.presets.save).not.toHaveBeenCalled()
  })

  it('keeps a slow mount of the previous template out of the next one\'s root', async () => {
    let release: () => void = () => undefined
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    setUiModuleLoader(async (url) => ({
      mount: url.includes('/models/old/')
        ? async (root: ShadowRoot) => {
            await gate
            const p = document.createElement('p')
            p.textContent = 'old'
            root.append(p)
          }
        : (root: ShadowRoot) => {
            const p = document.createElement('p')
            p.textContent = 'new'
            root.append(p)
          },
    }))
    const props = { ui: UI, version: undefined, inputs: { params: {} }, onFailure: vi.fn() }
    const { container, rerender } = render(<TemplateUi slug="old" deps={deps()} {...props} />)
    await waitFor(() => expect(container.querySelector('[data-testid="template-ui"]')).not.toBeNull())
    rerender(<TemplateUi slug="next" deps={deps()} {...props} />)
    await waitFor(() => expect(shadow(container).textContent).toBe('new'))
    release()
    await gate
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(shadow(container).textContent).toBe('new')
    expect(container.querySelectorAll('[data-testid="template-ui"]')).toHaveLength(1)
  })
})
