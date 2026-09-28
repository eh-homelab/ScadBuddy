import { beforeEach, describe, expect, it, vi } from 'vitest'
import { downloadBlob, isEmbedded, openExternal, triggerDownload } from './embed'

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

describe('triggerDownload', () => {
  let clicked: HTMLAnchorElement | undefined

  beforeEach(() => {
    clicked = undefined
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      // eslint-disable-next-line @typescript-eslint/no-this-alias -- the spy's receiver is the anchor under test
      clicked = this
    })
  })

  it('uses a download anchor', () => {
    triggerDownload('blob:x', 'model.3mf', false)
    expect(clicked?.getAttribute('download')).toBe('model.3mf')
    expect(clicked?.target).toBe('')
  })

  it('escapes the sandbox through a blank popup when embedded', () => {
    const popup = document.implementation.createHTMLDocument('popup')
    const close = vi.fn()
    const open = vi.spyOn(window, 'open').mockReturnValue({ document: popup, close } as unknown as Window)
    vi.useFakeTimers()
    triggerDownload('blob:x', 'model.3mf', true)
    expect(open).toHaveBeenCalledWith('', '_blank')
    expect(clicked?.ownerDocument).toBe(popup)
    expect(clicked?.getAttribute('download')).toBe('model.3mf')
    vi.advanceTimersByTime(1000)
    expect(close).toHaveBeenCalled()
    vi.useRealTimers()
    open.mockRestore()
  })

  it('falls back to a new-tab anchor when the popup is blocked', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    triggerDownload('blob:x', 'model.3mf', true)
    expect(clicked?.ownerDocument).toBe(document)
    expect(clicked?.target).toBe('_blank')
    open.mockRestore()
  })

  it('removes the anchor again', () => {
    triggerDownload('blob:x', 'model.3mf', false)
    expect(document.body.querySelector('a')).toBeNull()
  })
})

describe('downloadBlob', () => {
  it('opens the popup before the file has loaded, then saves into it', async () => {
    const popup = document.implementation.createHTMLDocument('popup')
    const open = vi.spyOn(window, 'open').mockReturnValue({ document: popup, close: vi.fn() } as unknown as Window)
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:y')
    let clicked: HTMLAnchorElement | undefined
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      // eslint-disable-next-line @typescript-eslint/no-this-alias -- the spy's receiver is the anchor under test
      clicked = this
    })
    let release: (blob: Blob) => void = () => {}
    const saving = downloadBlob(() => new Promise<Blob>((resolve) => (release = resolve)), 'a.3mf', true)
    expect(open).toHaveBeenCalledWith('', '_blank')
    expect(clicked).toBeUndefined()
    release(new Blob(['x']))
    await saving
    expect(clicked?.ownerDocument).toBe(popup)
    expect(clicked?.href).toBe('blob:y')
    open.mockRestore()
  })

  it('closes the popup when the file cannot be loaded', async () => {
    const close = vi.fn()
    const open = vi.spyOn(window, 'open').mockReturnValue({ document, close } as unknown as Window)
    await expect(downloadBlob(() => Promise.reject(new Error('HTTP 500')), 'a.3mf', true)).rejects.toThrow('HTTP 500')
    expect(close).toHaveBeenCalled()
    open.mockRestore()
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
