import { expect, test, type Locator } from '@playwright/test'

/** The DOM this file reads, typed here because the e2e build has no DOM lib. */
interface Box {
  left: number
  right: number
  top: number
  bottom: number
}
interface Piece {
  tagName: string
  textContent: string | null
  scrollWidth: number
  clientWidth: number
  getAttribute(name: string): string | null
  getBoundingClientRect(): Box
  getClientRects(): { length: number }
  querySelector(selector: string): Piece | null
  querySelectorAll(selector: string): Iterable<Piece>
  closest(selector: string): Piece | null
}

/**
 * #934 — every piece of the action bar, as boxes, with the pairs that overlap and the
 * text that is cut off. Read in the page because only it has layout.
 */
async function collisions(bar: Locator): Promise<{ overlaps: string[]; clipped: string[] }> {
  return bar.evaluate((root) => {
    const footer = root as unknown as Piece
    // The leaves a reader sees: controls, the colour chips, and text-bearing spans.
    const pieces = [...footer.querySelectorAll('button, select, label, li, span')].filter(
      (element) =>
        element.getClientRects().length > 0 &&
        !element.querySelector('button, select, label, li, span') &&
        !element.closest('button, li'),
    )
    const name = (element: Piece) =>
      `${element.tagName.toLowerCase()} "${(element.textContent ?? '').trim() || element.getAttribute('aria-label') || ''}"`
    const overlaps: string[] = []
    for (let i = 0; i < pieces.length; i += 1) {
      for (let j = i + 1; j < pieces.length; j += 1) {
        const a = pieces[i]!.getBoundingClientRect()
        const b = pieces[j]!.getBoundingClientRect()
        const x = Math.min(a.right, b.right) - Math.max(a.left, b.left)
        const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
        if (x > 1 && y > 1) overlaps.push(`${name(pieces[i]!)} × ${name(pieces[j]!)}`)
      }
    }
    const clipped = pieces
      .filter((element) => element.tagName === 'SPAN' && element.scrollWidth > element.clientWidth + 1)
      .map(name)
    return { overlaps, clipped }
  })
}

test.describe('action bar', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed')

  for (const width of [1440, 1100]) {
    test(`keeps the saved status clear of the project picker at ${width}px (#934)`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 })
      await page.goto('/m/name-keychain')
      const bar = page.locator('footer').filter({ has: page.getByTestId('generate') })

      await bar.getByLabel('Project').selectOption('1')
      const generate = page.getByTestId('generate')
      await expect(generate).toBeEnabled()
      await generate.click()
      await expect(bar.getByRole('button', { name: 'Open in Bambuddy' })).toBeVisible()

      expect(await collisions(bar)).toEqual({ overlaps: [], clipped: [] })
      // "2 colours" stays on one line.
      const colours = bar.getByText('2 colours')
      expect((await colours.boundingBox())!.height).toBeLessThan(20)
    })
  }
})
