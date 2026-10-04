import { afterEach, describe, expect, it, vi } from 'vitest'
import { installStaleChunkReload, loadOptionalChunk } from './staleChunks'

const TRACING = /\/tracing-[\w-]+\.js/

function preloadError(message?: string): Event {
  const event = new Event('vite:preloadError', { cancelable: true })
  if (message !== undefined) Object.assign(event, { payload: new TypeError(message) })
  window.dispatchEvent(event)
  return event
}

describe('installStaleChunkReload', () => {
  const installed: Array<() => void> = []

  /** One page load of `build`; a reload is a fresh install, as in the browser. */
  function load(build: string, reload = vi.fn()) {
    installed.splice(0).forEach((remove) => remove())
    installed.push(installStaleChunkReload(window, reload, build))
    return reload
  }

  afterEach(() => {
    installed.splice(0).forEach((remove) => remove())
    sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('reloads once when a chunk from an older build is missing', () => {
    const reload = load('old')
    const event = preloadError()
    expect(reload).toHaveBeenCalledTimes(1)
    expect(event.defaultPrevented).toBe(true)
  })

  it('does not loop when the reload brings back the same build', () => {
    const reload = load('old')
    preloadError()
    load('old', reload)
    const event = preloadError()
    expect(reload).toHaveBeenCalledTimes(1)
    expect(event.defaultPrevented).toBe(false)
  })

  it('stops after the new build fails too, however slow the reloads are', () => {
    const reload = load('old')
    preloadError()
    load('new', reload)
    preloadError()
    load('new', reload)
    const event = preloadError()
    expect(reload).toHaveBeenCalledTimes(2)
    expect(event.defaultPrevented).toBe(false)
  })

  it('recovers again after a later deploy in the same tab', () => {
    const reload = load('old')
    preloadError()
    load('new', reload)
    load('newer', reload)
    preloadError()
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('does not reload when sessionStorage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    const reload = load('old')
    const event = preloadError()
    expect(reload).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
  })

  it('does not reload for an optional chunk that fails, and still does for another', async () => {
    const reload = load('old')
    let rejectImport: (error: Error) => void = () => undefined
    const pending = loadOptionalChunk(
      () => new Promise<never>((_, reject) => (rejectImport = reject)),
      TRACING,
    ).catch(() => 'failed')
    const optional = preloadError('Failed to fetch dynamically imported module: https://x.test/assets/tracing-AbC123.js')
    rejectImport(new Error('blocked'))
    await expect(pending).resolves.toBe('failed')
    expect(reload).not.toHaveBeenCalled()
    expect(optional.defaultPrevented).toBe(false)
    // The failed optional chunk used up nothing: a stale chunk afterwards still reloads.
    preloadError()
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it("still reloads for another chunk's error while an optional load is pending", async () => {
    const reload = load('old')
    let rejectImport: (error: Error) => void = () => undefined
    const pending = loadOptionalChunk(
      () => new Promise<never>((_, reject) => (rejectImport = reject)),
      TRACING,
    ).catch(() => 'failed')
    const other = preloadError('Failed to fetch dynamically imported module: https://x.test/assets/ModelPage-AbC123.js')
    expect(reload).toHaveBeenCalledTimes(1)
    expect(other.defaultPrevented).toBe(true)
    rejectImport(new Error('blocked'))
    await pending
  })
})

describe('public/', () => {
  // The server caches everything under assets/ for a year as immutable, because Vite
  // content-hashes what it writes there. A public/assets/ file would land there unhashed.
  it('has no assets/ folder', () => {
    expect(Object.keys(import.meta.glob('/public/assets/**'))).toEqual([])
  })
})
