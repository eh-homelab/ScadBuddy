import { act } from 'react'
import { createPortal } from 'react-dom'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'

const domToCanvas = vi.fn()
vi.mock('modern-screenshot', () => ({ domToCanvas }))

const { hiddenElements, SCREENSHOT_MAX_BYTES, SCREENSHOT_MAX_EDGE, takeScreenshot } = await import('./screenshot')

/** A canvas as modern-screenshot hands it back: jsdom draws nothing, so this records the paint. */
function fakeCanvas(width: number, height: number, sizes: Record<string, number> = {}) {
  const painted: number[][] = []
  const context = { fillStyle: '', fillRect: (...rect: number[]) => painted.push(rect) }
  const canvas = {
    width,
    height,
    getContext: () => context,
    toBlob: (done: (blob: Blob | null) => void, type: string) =>
      done(new Blob([new Uint8Array(sizes[type] ?? 10)], { type })),
  }
  return { canvas, painted }
}

function rect(element: Element, left: number, top: number, width: number, height: number) {
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue({
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    x: left,
    y: top,
    toJSON: () => ({}),
  })
}

afterEach(() => {
  document.body.innerHTML = ''
  domToCanvas.mockReset()
  vi.restoreAllMocks()
})

describe('hiddenElements', () => {
  it('paints over passwords, user-only controls and what they control, and the assistant panel, each once', () => {
    document.body.innerHTML = `
      <main>
        <input id="key" type="password" />
        <div id="wrap" data-agent-user-only><input id="code" /><button id="confirm">Print</button></div>
        <button id="trigger" data-agent-user-only aria-controls="menu">Token</button>
        <button id="free" aria-controls="other">Render</button>
        <input id="width" />
      </main>
      <aside id="panel" aria-label="Assistant"><p>my chats</p></aside>
      <div id="menu">token value</div>
      <div id="other">fine</div>`
    const ids = hiddenElements().map((element) => element.id)
    expect(ids.sort()).toEqual(['key', 'menu', 'panel', 'trigger', 'wrap'])
  })
})

describe('hiddenElements, for what the assistant panel renders through a portal', () => {
  it('paints over a dialog the panel portals under <body>', async () => {
    const host = document.createElement('div')
    document.body.append(host)
    const root = createRoot(host)
    await act(async () =>
      root.render(
        <aside aria-label="Assistant">
          {createPortal(<div id="lightbox">a sent image</div>, document.body)}
        </aside>,
      ),
    )
    const ids = hiddenElements().map((element) => element.id || element.tagName.toLowerCase())
    expect(ids.sort()).toEqual(['aside', 'lightbox'])
    await act(async () => root.unmount())
  })
})

describe('takeScreenshot', () => {
  it("draws the body at the viewport's size, no larger than the long-side cap, and paints the hidden regions", async () => {
    vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(3136)
    vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(1000)
    document.body.innerHTML = '<input id="key" type="password" /><input id="off" type="password" />'
    rect(document.getElementById('key')!, 100, 50, 200, 20)
    // Not laid out (jsdom's default): not painted, not counted.
    const { canvas, painted } = fakeCanvas(1568, 500)
    domToCanvas.mockResolvedValue(canvas)
    const shot = await takeScreenshot()
    expect(domToCanvas).toHaveBeenCalledWith(document.body, expect.objectContaining({ width: 3136, height: 1000, scale: SCREENSHOT_MAX_EDGE / 3136 }))
    expect(painted).toEqual([[50, 25, 100, 10]])
    expect(shot).toMatchObject({ width: 1568, height: 500, hidden: 1 })
    expect(shot.image.type).toBe('image/png')
  })

  it('falls back to a JPEG for a PNG too large, and fails past the cap either way', async () => {
    vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(800)
    vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(600)
    domToCanvas.mockResolvedValueOnce(fakeCanvas(800, 600, { 'image/png': SCREENSHOT_MAX_BYTES + 1 }).canvas)
    expect((await takeScreenshot()).image.type).toBe('image/jpeg')
    domToCanvas.mockResolvedValueOnce(
      fakeCanvas(800, 600, { 'image/png': SCREENSHOT_MAX_BYTES + 1, 'image/jpeg': SCREENSHOT_MAX_BYTES + 1 }).canvas,
    )
    await expect(takeScreenshot()).rejects.toThrow(/over the/)
  })
})
