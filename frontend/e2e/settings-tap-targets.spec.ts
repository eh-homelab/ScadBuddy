import { expect, test, type Page } from '@playwright/test'

// #1035 — Settings at a phone's and a tablet's width: every control a finger has to hit is
// at least 24 px each way (WCAG 2.2 target size). Inline text links are exempt, as there.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

async function smallTargets(page: Page): Promise<string[]> {
  return (await page.evaluate(`(() => {
    const out = []
    for (const el of document.querySelectorAll('button, input[type="checkbox"], input[type="radio"], summary')) {
      if (el.closest('[aria-hidden="true"], [hidden], nav')) continue
      const box = el.getBoundingClientRect()
      if (box.width === 0 || box.height === 0) continue
      if (getComputedStyle(el).visibility === 'hidden') continue
      if (box.width < 24 || box.height < 24) {
        const label = el.getAttribute('aria-label') || el.id || el.textContent.trim().slice(0, 40) || el.tagName
        out.push(el.tagName.toLowerCase() + ' ' + label + ' ' + Math.round(box.width) + 'x' + Math.round(box.height))
      }
    }
    return out
  })()`)) as string[]
}

for (const [width, height] of [
  [390, 844],
  [768, 1024],
] as const) {
  test(`Settings controls are at least 24 px at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height })
    await page.goto('/settings')
    await expect(page.getByRole('table', { name: 'Deployment values' })).toBeAttached()
    await expect(page.getByRole('listitem', { name: 'Built-in plugin scadbuddy' })).toBeAttached()
    // Open every disclosure, so what is inside one is measured too.
    // Each click takes one out of the closed set, so it is always the first that is left.
    const closed = page.locator('main details:not([open]) > summary')
    while ((await closed.count()) > 0) await closed.first().click()
    // A plugin package's files (#1029) open with a button, not a summary.
    const files = page.locator('main').getByRole('button', { name: /^Files to read/, expanded: false })
    while ((await files.count()) > 0) await files.first().click()
    await expect(page.getByRole('list', { name: 'Files to read in scadbuddy' })).toBeVisible()
    expect(await smallTargets(page)).toEqual([])
  })
}
