import { afterEach, describe, expect, it, vi } from 'vitest'
import { installStaleChunkReload } from './staleChunks'

function preloadError(): Event {
  const event = new Event('vite:preloadError', { cancelable: true })
  window.dispatchEvent(event)
  return event
}

describe('installStaleChunkReload', () => {
  const installed: Array<() => void> = []

  function install(now: () => number) {
    const reload = vi.fn()
    installed.push(installStaleChunkReload(window, reload, now))
    return reload
  }

  afterEach(() => {
    installed.splice(0).forEach((remove) => remove())
    sessionStorage.clear()
    vi.restoreAllMocks()
  })

  it('reloads once when a chunk from an older build is missing', () => {
    const reload = install(() => 1_000_000)
    const event = preloadError()
    expect(reload).toHaveBeenCalledTimes(1)
    expect(event.defaultPrevented).toBe(true)
  })

  it('does not loop when the reloaded page fails again straight away', () => {
    let time = 1_000_000
    const reload = install(() => time)
    preloadError()
    time += 2_000
    const event = preloadError()
    expect(reload).toHaveBeenCalledTimes(1)
    expect(event.defaultPrevented).toBe(false)
  })

  it('recovers again after a later deploy in the same tab', () => {
    let time = 1_000_000
    const reload = install(() => time)
    preloadError()
    time += 60 * 60 * 1000
    preloadError()
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('does not reload when sessionStorage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    const reload = install(() => 1_000_000)
    const event = preloadError()
    expect(reload).not.toHaveBeenCalled()
    expect(event.defaultPrevented).toBe(false)
  })
})
