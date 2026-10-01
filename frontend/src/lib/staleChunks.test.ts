import { afterEach, describe, expect, it, vi } from 'vitest'
import { installStaleChunkReload } from './staleChunks'

function preloadError(): Event {
  const event = new Event('vite:preloadError', { cancelable: true })
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
})
