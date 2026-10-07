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

/**
 * Every vertical scroller (`overflow-y-auto`, a page's own scroll area among them) that also
 * scrolls sideways. CSS gives such a box `overflow-x: auto` too, so content wider than it
 * pans the whole page, and `offscreen` reads it as a deliberate sideways scroller (#1036).
 */
async function sidewaysScrollers(page: Page): Promise<string[]> {
  return (await page.evaluate(`[...document.querySelectorAll('[class~="overflow-y-auto"]')]
    .filter((el) => el.scrollWidth > el.clientWidth + 1)
    .map((el) => el.className + ' ' + el.scrollWidth + '>' + el.clientWidth)`)) as string[]
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
    // #1741 — behind More on a phone, which keeps the action bar to one row.
    await page.getByTestId('more-actions').click()
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

test.describe('model lists at 390 px (#1036)', () => {
  test('Versions keeps a long commit message inside its card', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    // The message an agent or an editor writes, not the short default: the list sized to it.
    await page.evaluate(`(async () => {
      const url = '/api/v1/models/name-keychain/source'
      const source = await (await fetch(url)).text()
      const message = 'Thicken the raised text to 2 mm so the thin strokes of the letters survive printing'
      const put = await fetch(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source, message }),
      })
      if (!put.ok) throw new Error('save failed: ' + put.status)
    })()`)
    await page.getByRole('link', { name: 'Versions' }).click()
    const versions = page.getByTestId('versions')
    await expect(versions.locator('li').first()).toContainText('Thicken the raised text')
    expect(await sidewaysScrollers(page)).toEqual([])
    expect((await versions.boundingBox())!.width).toBeLessThanOrEqual(390)
    await expect(versions.getByText('current', { exact: true })).toBeInViewport({ ratio: 1 })
  })

  test('History keeps a changed value beside its long caption on screen', async ({ page }) => {
    await page.goto('/m/name-keychain/history')
    const value = page.locator('dd', { hasText: /^2\b/ }).first()
    await expect(value).toBeVisible()
    expect(await sidewaysScrollers(page)).toEqual([])
    await expect(value).toBeInViewport({ ratio: 1 })
    expect((await value.boundingBox())!.width).toBeGreaterThan(0)
  })
})
