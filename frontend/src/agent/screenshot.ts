import { isUserOnly } from './dom'

/**
 * `screenshot` (#254): the fallback for what `snapshot` cannot say (a layout, a colour, the
 * 3D preview), the viewport drawn by the page itself. `modern-screenshot` re-renders the
 * DOM into a canvas through an SVG `foreignObject`, keeping scroll positions; it loads
 * only when a screenshot is asked for, so it stays out of the entry chunk. The preview's
 * WebGL canvas keeps its drawing buffer (Preview.tsx `preserveDrawingBuffer`), so it
 * comes out as drawn.
 *
 * What `snapshot` never reads back is painted over, never drawn: password fields, and
 * everything only the user may touch (`USER_ONLY`, `isUserOnly`), which holds a
 * pairing code being typed (#746) and a just-created MCP token. So is the assistant
 * panel: a paired agent is not shown the user's chats with another.
 *
 * The image is a `Blob`; `link.ts` sends its bytes as binary frames and the answer names
 * it, so it never travels as base64 in JSON (#2479).
 */

/** The longest side, in pixels, of the image: what the Messages API takes without scaling down. */
export const SCREENSHOT_MAX_EDGE = 1568
/** The largest image sent: the agent's `IMAGE_BYTES_MAX` (sessions/images.ts), 5 MiB of base64. */
export const SCREENSHOT_MAX_BYTES = 3_932_160
/** The colour hidden regions are painted. */
const HIDDEN_FILL = '#6b7280'

export interface Screenshot {
  image: Blob
  /** The image's size, in pixels. */
  width: number
  height: number
  /** How many regions were painted over. */
  hidden: number
}

/** The elements a screenshot paints over (exported for the tests; layout is not jsdom's). */
export function hiddenElements(doc: Document = document): Element[] {
  const found = new Set<Element>()
  for (const element of doc.querySelectorAll('[data-agent-user-only], input[type="password"], aside[aria-label="Assistant"]')) {
    found.add(element)
  }
  // What a user-only control opened or rendered elsewhere: a portal's root sits under
  // <body>, and a trigger names its content with aria-controls (dom.ts `isUserOnly`).
  for (const element of doc.body.children) if (!found.has(element) && isUserOnly(element)) found.add(element)
  for (const controller of doc.querySelectorAll('[aria-controls]')) {
    if (!isUserOnly(controller)) continue
    for (const id of (controller.getAttribute('aria-controls') ?? '').split(/\s+/)) {
      const target = id ? doc.getElementById(id) : null
      if (target) found.add(target)
    }
  }
  // An element inside another one painted over adds nothing.
  return [...found].filter((element) => ![...found].some((other) => other !== element && other.contains(element)))
}

function encode(canvas: HTMLCanvasElement, type: string, quality?: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality))
}

export async function takeScreenshot(doc: Document = document): Promise<Screenshot> {
  const view = doc.defaultView
  if (!view) throw new Error('the page has no window to draw')
  const width = view.innerWidth
  const height = view.innerHeight
  const scale = Math.min(1, SCREENSHOT_MAX_EDGE / Math.max(width, height, 1))
  const hidden = hiddenElements(doc).map((element) => element.getBoundingClientRect())
  const { domToCanvas } = await import('modern-screenshot')
  const body = getComputedStyle(doc.body).backgroundColor
  const canvas = await domToCanvas(doc.body, {
    width,
    height,
    scale,
    backgroundColor: body === 'rgba(0, 0, 0, 0)' ? getComputedStyle(doc.documentElement).backgroundColor : body,
    // The body drawn from the window's scroll position, as the viewport shows it.
    ...(view.scrollX || view.scrollY ? { style: { transform: `translate(${-view.scrollX}px, ${-view.scrollY}px)` } } : {}),
  })
  const context = canvas.getContext('2d')
  if (!context) throw new Error('the page could not draw the screenshot')
  const ratio = canvas.width / width
  context.fillStyle = HIDDEN_FILL
  // Only what is in view: a region off screen or not laid out is not counted.
  const shown = hidden.filter((r) => r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 && r.left < width && r.top < height)
  for (const rect of shown) context.fillRect(rect.left * ratio, rect.top * ratio, rect.width * ratio, rect.height * ratio)
  let image = await encode(canvas, 'image/png')
  // A busy page can be a large PNG; a JPEG of it is not.
  if (image && image.size > SCREENSHOT_MAX_BYTES) image = await encode(canvas, 'image/jpeg', 0.85)
  if (!image) throw new Error('the page could not encode the screenshot')
  if (image.size > SCREENSHOT_MAX_BYTES) {
    throw new Error(`the screenshot is ${image.size} bytes, over the ${SCREENSHOT_MAX_BYTES} an image may be`)
  }
  return { image, width: canvas.width, height: canvas.height, hidden: shown.length }
}
