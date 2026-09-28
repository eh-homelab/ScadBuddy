import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DownloadBlockedError, downloadBlob, isEmbedded, openExternal } from './embed'

describe('isEmbedded', () => {
  it('is false at the top level', () => {
    expect(isEmbedded()).toBe(false)
  })

  it('is true inside a frame', () => {
    const top = window.top
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    expect(isEmbedded()).toBe(true)
    Object.defineProperty(window, 'top', { value: top, configurable: true })
  })
})

describe('downloadBlob', () => {
  let clicked: HTMLAnchorElement | undefined

  beforeEach(() => {
    clicked = undefined
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:y')
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      // eslint-disable-next-line @typescript-eslint/no-this-alias -- the spy's receiver is the anchor under test
      clicked = this
    })
  })

  afterEach(() => vi.restoreAllMocks())

  it('uses a plain download anchor at the top level, and removes it again', async () => {
    const open = vi.spyOn(window, 'open')
    await downloadBlob(async () => new Blob(['x']), 'model.3mf', false)
    expect(open).not.toHaveBeenCalled()
    expect(clicked?.ownerDocument).toBe(document)
    expect(clicked?.getAttribute('download')).toBe('model.3mf')
    expect(clicked?.href).toBe('blob:y')
    expect(clicked?.target).toBe('')
    expect(document.body.querySelector('a')).toBeNull()
  })

  it('opens the popup before the file has loaded when embedded, saves into it, then closes it', async () => {
    const popup = document.implementation.createHTMLDocument('popup')
    const close = vi.fn()
    const open = vi.spyOn(window, 'open').mockReturnValue({ document: popup, close } as unknown as Window)
    let release: (blob: Blob) => void = () => {}
    const saving = downloadBlob(() => new Promise<Blob>((resolve) => (release = resolve)), 'a.3mf', true)
    expect(open).toHaveBeenCalledWith('', '_blank')
    expect(clicked).toBeUndefined()
    vi.useFakeTimers()
    release(new Blob(['x']))
    await saving
    expect(clicked?.ownerDocument).toBe(popup)
    expect(clicked?.href).toBe('blob:y')
    vi.advanceTimersByTime(1000)
    expect(close).toHaveBeenCalled()
    vi.useRealTimers()
  })

  it('refuses, without loading, when the popup is blocked inside the frame', async () => {
    // The frame's own anchor would be dropped silently (#612), so it is not tried.
    vi.spyOn(window, 'open').mockReturnValue(null)
    const load = vi.fn(async () => new Blob(['x']))
    const saving = downloadBlob(load, 'a.3mf', true)
    await expect(saving).rejects.toBeInstanceOf(DownloadBlockedError)
    await expect(saving).rejects.toThrow(/pop-ups/)
    expect(load).not.toHaveBeenCalled()
    expect(clicked).toBeUndefined()
  })

  it('closes the popup when the file cannot be loaded', async () => {
    const close = vi.fn()
    vi.spyOn(window, 'open').mockReturnValue({ document, close } as unknown as Window)
    await expect(downloadBlob(() => Promise.reject(new Error('HTTP 500')), 'a.3mf', true)).rejects.toThrow('HTTP 500')
    expect(close).toHaveBeenCalled()
  })
})

describe('openExternal', () => {
  it('opens a new tab only when embedded', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    openExternal('https://bambuddy.example/queue', true)
    expect(open).toHaveBeenLastCalledWith('https://bambuddy.example/queue', '_blank', 'noopener')
    openExternal('https://bambuddy.example/queue', false)
    expect(open).toHaveBeenLastCalledWith('https://bambuddy.example/queue', '_self', 'noopener')
    open.mockRestore()
  })
})
