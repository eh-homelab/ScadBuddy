import type { Locator } from '@playwright/test'

/**
 * A screenshot of ``canvas`` once two in a row agree. Fails if it never settles: a frame
 * taken mid-motion would otherwise be read later as the camera having moved, or not
 * (#1818: share-image and preview-overlays each had a copy, and only one failed).
 */
export async function settled(canvas: Locator): Promise<Buffer> {
  let last = await canvas.screenshot()
  for (let tries = 0; tries < 20; tries += 1) {
    await canvas.page().waitForTimeout(150)
    const next = await canvas.screenshot()
    if (next.equals(last)) return next
    last = next
  }
  throw new Error('the canvas never settled: 20 screenshots 150 ms apart all differed')
}
