import { act, fireEvent, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { leaveFullscreen, useFullscreen } from './useFullscreen'

let element: HTMLDivElement | undefined

function mount() {
  element = document.createElement('div')
  document.body.append(element)
  const ref = { current: element }
  const { result } = renderHook(() => useFullscreen(ref))
  return { element, result }
}

/**
 * jsdom has no Fullscreen API. This is the part of it the hook uses, driven the way a
 * browser drives it: the change event fires before the request settles.
 */
function offerFullscreen(target: HTMLElement, { refuse = false } = {}) {
  let current: Element | null = null
  const change = (next: Element | null) => {
    current = next
    document.dispatchEvent(new Event('fullscreenchange'))
  }
  Object.defineProperty(document, 'fullscreenEnabled', { configurable: true, value: true })
  Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => current })
  const exit = vi.fn(async () => change(null))
  document.exitFullscreen = exit
  const request = vi.fn(async () => {
    if (refuse) throw new TypeError('Permissions check failed')
    change(target)
  })
  target.requestFullscreen = request
  return { request, exit, enter: () => change(target), leave: () => change(null) }
}

afterEach(() => {
  for (const name of ['fullscreenEnabled', 'fullscreenElement', 'exitFullscreen']) {
    Reflect.deleteProperty(document, name)
  }
  element?.remove()
  element = undefined
})

describe('useFullscreen', () => {
  it('fills the window where the Fullscreen API is not offered', () => {
    // jsdom offers none, the same answer as Bambuddy's frame without allow="fullscreen".
    const { result } = mount()
    expect(result.current.mode).toBeNull()

    act(() => result.current.toggle())
    expect(result.current.mode).toBe('window')

    act(() => result.current.toggle())
    expect(result.current.mode).toBeNull()
  })

  it('leaves the window on Escape', () => {
    const { result } = mount()
    act(() => result.current.toggle())

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(result.current.mode).toBeNull()

    // Only while it fills the window: Escape means nothing to it otherwise.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(result.current.mode).toBeNull()
  })

  it('leaves an Escape that a handler nearer the focus already took', () => {
    const { element, result } = mount()
    const field = document.createElement('input')
    field.addEventListener('keydown', (event) => event.preventDefault())
    element.append(field)
    act(() => result.current.toggle())

    fireEvent.keyDown(field, { key: 'Escape' })
    expect(result.current.mode).toBe('window')
  })

  it('leaves an Escape to a dialog that opened after it', () => {
    const { element, result } = mount()
    act(() => result.current.toggle())
    // What `Dialog` does: a document listener, added when it opens, that takes the key.
    const dialog = (event: KeyboardEvent) => event.preventDefault()
    document.addEventListener('keydown', dialog)

    fireEvent.keyDown(element, { key: 'Escape' })
    document.removeEventListener('keydown', dialog)
    expect(result.current.mode).toBe('window')
  })

  it('uses the Fullscreen API where the page may', async () => {
    const { element, result } = mount()
    const api = offerFullscreen(element)

    await act(async () => result.current.toggle())
    expect(api.request).toHaveBeenCalledOnce()
    expect(result.current.mode).toBe('screen')

    await act(async () => result.current.toggle())
    expect(api.exit).toHaveBeenCalledOnce()
    expect(result.current.mode).toBeNull()
  })

  it('follows the browser out of full screen', async () => {
    const { element, result } = mount()
    const api = offerFullscreen(element)
    await act(async () => result.current.toggle())
    expect(result.current.mode).toBe('screen')

    // The browser's own Escape: only the change event says it happened.
    act(() => api.leave())
    expect(result.current.mode).toBeNull()
    expect(api.exit).not.toHaveBeenCalled()
  })

  it('asks once while a request is in flight, and leaves for the page', async () => {
    const { element, result } = mount()
    const api = offerFullscreen(element)
    // Settled only when the test says so, like a slow transition.
    let grant = () => {}
    api.request.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          grant = () => {
            api.enter()
            resolve()
          }
        }),
    )

    // A double click: the second press lands before the first is answered.
    act(() => result.current.toggle())
    act(() => result.current.toggle())
    expect(api.request).toHaveBeenCalledOnce()

    await act(async () => grant())
    expect(result.current.mode).toBe('screen')

    await act(async () => result.current.toggle())
    expect(result.current.mode).toBeNull()
  })

  it('drops the stand-in when the Fullscreen API takes over', async () => {
    const { element, result } = mount()
    const api = offerFullscreen(element, { refuse: true })
    await act(async () => result.current.toggle())
    expect(result.current.mode).toBe('window')

    // The element goes full screen after all; leaving that must reach the page, not a
    // stand-in left underneath.
    act(() => api.enter())
    expect(result.current.mode).toBe('screen')
    act(() => api.leave())
    expect(result.current.mode).toBeNull()
  })

  it('leaves either mode when asked from outside, and says whether it was in one', async () => {
    const { element, result } = mount()
    expect(leaveFullscreen()).toBe(false)

    act(() => result.current.toggle())
    expect(result.current.mode).toBe('window')
    let left = false
    act(() => {
      left = leaveFullscreen()
    })
    expect(left).toBe(true)
    expect(result.current.mode).toBeNull()

    const api = offerFullscreen(element)
    await act(async () => result.current.toggle())
    expect(result.current.mode).toBe('screen')
    act(() => {
      left = leaveFullscreen()
    })
    expect(left).toBe(true)
    expect(api.exit).toHaveBeenCalledOnce()
    expect(result.current.mode).toBeNull()
  })

  it('counts a request still in flight as full screen, and takes it back', async () => {
    const { element, result } = mount()
    const api = offerFullscreen(element)
    let grant = () => {}
    api.request.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          grant = () => {
            api.enter()
            resolve()
          }
        }),
    )
    act(() => result.current.toggle())

    // Asked before the browser has answered: the answer would cover whatever asked.
    let left = false
    act(() => {
      left = leaveFullscreen()
    })
    expect(left).toBe(true)

    await act(async () => grant())
    expect(api.exit).toHaveBeenCalledOnce()
    expect(result.current.mode).toBeNull()
  })

  it('does not fill the window for a refused request it had taken back', async () => {
    const { element, result } = mount()
    const api = offerFullscreen(element)
    let refuse = () => {}
    api.request.mockImplementation(
      () =>
        new Promise<void>((_, reject) => {
          refuse = () => reject(new TypeError('Permissions check failed'))
        }),
    )
    act(() => result.current.toggle())
    act(() => {
      leaveFullscreen()
    })

    await act(async () => refuse())
    expect(result.current.mode).toBeNull()
  })

  it('fills the window when the browser refuses full screen', async () => {
    const { element, result } = mount()
    const api = offerFullscreen(element, { refuse: true })

    await act(async () => result.current.toggle())
    expect(api.request).toHaveBeenCalledOnce()
    expect(result.current.mode).toBe('window')
  })
})
