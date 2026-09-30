import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  COPY_DENIED,
  COPY_FRAMED,
  COPY_REFUSED,
  COPY_UNDRAWN,
  copyImage,
  copyText,
} from './clipboard'

function node(text: string): HTMLElement {
  const element = document.createElement('code')
  element.textContent = text
  document.body.appendChild(element)
  return element
}

describe('copyText', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
    window.getSelection()?.removeAllRanges()
  })

  it('uses the async Clipboard API when it is allowed', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    expect(await copyText('sbmcp_abc', node('sbmcp_abc'))).toBe(true)
    expect(writeText).toHaveBeenCalledWith('sbmcp_abc')
  })

  it('falls back to execCommand when the frame may not write the clipboard', async () => {
    const writeText = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const execCommand = vi.fn().mockReturnValue(true)
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })
    const element = node('sbmcp_abc')
    expect(await copyText('sbmcp_abc', element)).toBe(true)
    expect(execCommand).toHaveBeenCalledWith('copy')
    expect(window.getSelection()?.toString()).toBe('sbmcp_abc')
  })

  it('leaves the text selected for a manual copy when nothing works', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined })
    Object.defineProperty(document, 'execCommand', { value: () => false, configurable: true })
    expect(await copyText('sbmcp_abc', node('sbmcp_abc'))).toBe(false)
    expect(window.getSelection()?.toString()).toBe('sbmcp_abc')
  })

  it('says it failed when there is no fallback node', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined })
    expect(await copyText('x')).toBe(false)
  })
})

describe('copyImage (#722)', () => {
  class FakeClipboardItem {
    readonly items: Record<string, Promise<Blob>>
    constructor(items: Record<string, Promise<Blob>>) {
      this.items = items
    }
  }
  const png = () => Promise.resolve(new Blob(['png'], { type: 'image/png' }))
  const refused = (message = 'Write permission denied.') =>
    new DOMException(message, 'NotAllowedError')

  function stub(write: ReturnType<typeof vi.fn>, permissions?: unknown) {
    vi.stubGlobal('ClipboardItem', FakeClipboardItem)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { write }, permissions })
  }

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('writes the image promise, so the write starts inside the click', async () => {
    const write = vi.fn().mockResolvedValue(undefined)
    const query = vi.fn()
    stub(write, { query })
    const image = png()
    expect(await copyImage(image, false)).toEqual({ ok: true })
    expect(write).toHaveBeenCalledTimes(1)
    const [item] = write.mock.calls[0]![0] as FakeClipboardItem[]
    expect(item!.items['image/png']).toBe(image)
    // Nothing is asked before the write, which would spend the gesture.
    expect(query).not.toHaveBeenCalled()
  })

  it('says to allow the clipboard in the site settings when access is denied', async () => {
    const query = vi.fn().mockResolvedValue({ state: 'denied' })
    stub(vi.fn().mockRejectedValue(refused()), { query })
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    expect(await copyImage(png(), false)).toEqual({ ok: false, message: COPY_DENIED })
    expect(query).toHaveBeenCalledWith({ name: 'clipboard-write' })
  })

  it.each(['prompt', 'granted'])('gives the plain refusal when the state is %s', async (state) => {
    stub(vi.fn().mockRejectedValue(refused()), { query: vi.fn().mockResolvedValue({ state }) })
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    expect(await copyImage(png(), false)).toEqual({ ok: false, message: COPY_REFUSED })
  })

  it('gives the plain refusal where the permission name throws or there is no API', async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    stub(vi.fn().mockRejectedValue(refused()), {
      query: vi.fn().mockRejectedValue(new TypeError("'clipboard-write' is not a valid name")),
    })
    expect(await copyImage(png(), false)).toEqual({ ok: false, message: COPY_REFUSED })
    stub(vi.fn().mockRejectedValue(refused()), undefined)
    expect(await copyImage(png(), false)).toEqual({ ok: false, message: COPY_REFUSED })
  })

  it("names Bambuddy's frame when framed cross-origin", async () => {
    const query = vi.fn().mockResolvedValue({ state: 'denied' })
    stub(vi.fn().mockRejectedValue(refused()), { query })
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    expect(await copyImage(png(), true)).toEqual({ ok: false, message: COPY_FRAMED })
  })

  it('retries once after focusing the window when the document lacked focus', async () => {
    const write = vi
      .fn()
      .mockRejectedValueOnce(refused('Document is not focused.'))
      .mockResolvedValueOnce(undefined)
    stub(write, { query: vi.fn() })
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    const focus = vi.spyOn(window, 'focus').mockImplementation(() => undefined)
    expect(await copyImage(png(), false)).toEqual({ ok: true })
    expect(focus).toHaveBeenCalledTimes(1)
    expect(write).toHaveBeenCalledTimes(2)
  })

  it('retries only once', async () => {
    const write = vi.fn().mockRejectedValue(refused('Document is not focused.'))
    stub(write, { query: vi.fn().mockResolvedValue({ state: 'prompt' }) })
    vi.spyOn(window, 'focus').mockImplementation(() => undefined)
    expect(await copyImage(png(), false)).toEqual({ ok: false, message: COPY_REFUSED })
    expect(write).toHaveBeenCalledTimes(2)
  })

  it('does not retry a refusal that is not about focus', async () => {
    const write = vi.fn().mockRejectedValue(new DOMException('bad type', 'DataError'))
    stub(write, { query: vi.fn().mockResolvedValue({ state: 'granted' }) })
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    expect(await copyImage(png(), false)).toEqual({ ok: false, message: COPY_REFUSED })
    expect(write).toHaveBeenCalledTimes(1)
  })

  it('says the image was not drawn when the drawing failed', async () => {
    const image = Promise.reject(new Error('no context'))
    stub(vi.fn().mockImplementation(async () => await image), { query: vi.fn() })
    expect(await copyImage(image, false)).toEqual({ ok: false, message: COPY_UNDRAWN })
  })
})
