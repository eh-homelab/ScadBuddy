import { act, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import type { HostDeps } from './host'
import { setUiModuleLoader } from './loadModule'
import { MOUNT_TIMEOUT_MS, TemplateUi } from './TemplateUi'
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

  it('falls back when mount never settles, and undoes a mount that finishes late (#847)', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      let called: () => void = () => {}
      const mounted = new Promise<void>((resolve) => {
        called = resolve
      })
      let finish: (cleanup: () => void) => void = () => {}
      withModule(() => {
        called()
        return new Promise((resolve) => {
          finish = resolve
        })
      })
      const onFailure = vi.fn()
      render(<TemplateUi slug="name-keychain" ui={UI} version={undefined} deps={deps()} inputs={{ params: {} }} onFailure={onFailure} />)
      await mounted
      await vi.advanceTimersByTimeAsync(MOUNT_TIMEOUT_MS - 1)
      expect(onFailure).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(1)
      expect(onFailure).toHaveBeenCalledWith({ file: 'ui/index.js', message: expect.stringContaining('did not load and mount') })
      const cleanup = vi.fn()
      await act(async () => finish(cleanup))
      expect(cleanup).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('falls back when the module import never settles, and never mounts it late (#847)', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      let requested: () => void = () => {}
      const loading = new Promise<void>((resolve) => {
        requested = resolve
      })
      let answer: (module: unknown) => void = () => {}
      setUiModuleLoader(() => {
        requested()
        return new Promise((resolve) => {
          answer = resolve
        })
      })
      const mount = vi.fn()
      const onFailure = vi.fn()
      render(<TemplateUi slug="name-keychain" ui={UI} version={undefined} deps={deps()} inputs={{ params: {} }} onFailure={onFailure} />)
      await loading
      await vi.advanceTimersByTimeAsync(MOUNT_TIMEOUT_MS)
      expect(onFailure).toHaveBeenCalledWith({ file: 'ui/index.js', message: expect.stringContaining('did not load and mount') })
      await act(async () => answer({ mount }))
      expect(mount).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('refuses an unsupported api major without loading anything', async () => {
    const urls = withModule(vi.fn())
    const onFailure = vi.fn()
    render(<TemplateUi slug="s" ui={{ ...UI, api: 7 }} version={undefined} deps={deps()} inputs={{ params: {} }} onFailure={onFailure} />)
    await waitFor(() => expect(onFailure).toHaveBeenCalledWith({ file: 'ui/index.js', message: expect.stringMatching(/API 7.*supports 1/) }))
    expect(urls).toEqual([])
  })

  it('refuses a module path outside ui/ without loading it', async () => {
    const urls = withModule(vi.fn())
    const onFailure = vi.fn()
    render(
      <TemplateUi slug="s" ui={{ ...UI, module: 'ui/../model.scad' }} version={undefined} deps={deps()} inputs={{ params: {} }} onFailure={onFailure} />,
    )
    await waitFor(() => expect(onFailure).toHaveBeenCalledWith({ file: 'ui/../model.scad', message: expect.stringContaining('not a file under ui/') }))
    expect(urls).toEqual([])
  })

  it('keeps the deps it mounted with when only the deps prop changes (HostDeps members are stable)', async () => {
    let kept: Host | undefined
    const mount = vi.fn<Mount>((_root, host) => {
      kept = host
    })
    withModule(mount)
    const a = deps()
    const b = { ...deps(), getInputs: vi.fn(() => ({ params: { name: 'B' } })) }
    const props = { slug: 's', ui: UI, version: undefined, inputs: { params: {} }, onFailure: vi.fn() }
    const { rerender } = render(<TemplateUi {...props} deps={a} />)
    await waitFor(() => expect(kept).toBeDefined())
    rerender(<TemplateUi {...props} deps={b} />)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(mount).toHaveBeenCalledOnce()
    expect((kept as Host).inputs.get()).toEqual({ params: { name: 'Hi' } })
    expect(b.getInputs).not.toHaveBeenCalled()
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

  it('leaves a mount that resolves after unmount no page and no working host', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    const cleanup = vi.fn()
    let started = false
    let late: Host | undefined
    withModule(async (root, host) => {
      started = true
      await gate
      root.append(document.createElement('span'))
      late = host
      return cleanup
    })
    const d = deps()
    const { container, unmount } = render(
      <TemplateUi slug="s" ui={UI} version={undefined} deps={d} inputs={{ params: {} }} onFailure={vi.fn()} />,
    )
    await waitFor(() => expect(started).toBe(true))
    const root = shadow(container)
    unmount()
    release()
    await waitFor(() => expect(cleanup).toHaveBeenCalledOnce())
    expect(root.host.isConnected).toBe(false)
    expect(container.childNodes.length).toBe(0)
    late?.inputs.set({ params: { name: 'late' } })
    expect(d.setInputs).not.toHaveBeenCalled()
    await expect(late!.generate()).rejects.toThrow(/unmounted/)
    await expect(late!.presets.load('p')).rejects.toThrow(/unmounted/)
    expect(d.presets.load).not.toHaveBeenCalled()
  })

  it('keeps a mount still in flight at a template switch out of the next one', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    let started = false
    const paragraph = (text: string) => Object.assign(document.createElement('p'), { textContent: text })
    setUiModuleLoader(async (url) =>
      url.includes('/models/a/')
        ? {
            mount: async (root: ShadowRoot) => {
              started = true
              await gate
              root.append(paragraph('A'))
            },
          }
        : { mount: (root: ShadowRoot) => root.append(paragraph('B')) },
    )
    const props = { ui: UI, version: undefined, deps: deps(), inputs: { params: {} }, onFailure: vi.fn() }
    const { container, rerender } = render(<TemplateUi slug="a" {...props} />)
    await waitFor(() => expect(started).toBe(true))
    rerender(<TemplateUi slug="b" {...props} />)
    await waitFor(() => expect(shadow(container).textContent).toBe('B'))
    release()
    await act(async () => {
      await gate
      await new Promise((resolve) => setTimeout(resolve, 0))
    })
    expect(shadow(container).textContent).toBe('B')
  })

  it('reports an import that rejects and still mounts the next module', async () => {
    setUiModuleLoader(async () => {
      throw new TypeError('Failed to fetch dynamically imported module')
    })
    const onFailure = vi.fn()
    const props = { slug: 's', ui: UI, deps: deps(), inputs: { params: {} }, onFailure }
    const { container, rerender } = render(<TemplateUi {...props} version={undefined} />)
    await waitFor(() =>
      expect(onFailure).toHaveBeenCalledWith({
        file: 'ui/index.js',
        message: 'Failed to fetch dynamically imported module',
      }),
    )
    withModule((root) => {
      root.append(document.createElement('span'))
    })
    rerender(<TemplateUi {...props} version="abc1234" />)
    await waitFor(() => expect(shadow(container).childNodes.length).toBe(1))
    expect(onFailure).toHaveBeenCalledOnce()
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
})
