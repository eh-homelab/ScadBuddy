import { expect, test, type Page } from '@playwright/test'

// #362 — the customize page at a phone's width. msw-backed: the templates are fixtures.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

test.use({ viewport: { width: 390, height: 844 } })

/**
 * Every visible control on the page, open shadow roots included (a template UI mounts
 * in one), that does not lie inside the screen: the page cannot scroll sideways, so
 * those are out of reach.
 */
async function offscreen(page: Page): Promise<string[]> {
  return (await page.evaluate(`(() => {
    const width = document.documentElement.clientWidth
    const out = []
    const visit = (root) => {
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) visit(el.shadowRoot)
        if (!el.matches('button, a[href], input, select, textarea, [role="tab"]')) continue
        if (el.closest('[aria-hidden="true"], [hidden]')) continue
        const box = el.getBoundingClientRect()
        if (box.width === 0 || box.height === 0) continue
        if (getComputedStyle(el).visibility === 'hidden') continue
        // Clipped on purpose by a scroller of its own (a scrolling tab row) is reachable.
        let clipped = false
        for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
          const s = getComputedStyle(up)
          if (/(auto|scroll)/.test(s.overflowX) && up.scrollWidth > up.clientWidth + 1) { clipped = true; break }
        }
        if (clipped) continue
        if (box.left < -0.5 || box.right > width + 0.5) {
          const label = el.getAttribute('aria-label') || el.textContent.trim().slice(0, 30) || el.getAttribute('name') || el.tagName
          out.push(label + ' ' + Math.round(box.left) + '..' + Math.round(box.right))
        }
      }
    }
    visit(document)
    return out
  })()`)) as string[]
}

async function pageWidth(page: Page): Promise<number> {
  return Number(await page.evaluate('document.documentElement.scrollWidth'))
}

test.describe('customize page at 390 px (#362)', () => {
  test('a generated form keeps every control on screen', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    expect(await pageWidth(page)).toBeLessThanOrEqual(390)
    expect(await offscreen(page)).toEqual([])
  })

  test('after Generate, Print and the downloads are on screen', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByTestId('generate').click()
    await expect(page.getByText(/^Saved /)).toBeVisible()
    await expect(page.getByTestId('print')).toBeInViewport()
    expect(await pageWidth(page)).toBeLessThanOrEqual(390)
    expect(await offscreen(page)).toEqual([])
  })

  test("a template's own panel UI fits the screen", async ({ page }) => {
    await page.goto('/m/builtin%3Amaze-puzzle')
    await expect(page.getByTestId('ui-origin')).toBeVisible()
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    expect(await pageWidth(page)).toBeLessThanOrEqual(390)
    expect(await offscreen(page)).toEqual([])
  })
})
