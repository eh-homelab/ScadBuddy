import { expect, test, type Page } from '@playwright/test'

// #1035 — Settings at a phone's and a tablet's width: every control a finger has to hit is
// at least 24 px each way (WCAG 2.2 target size). Inline text links are exempt, as there.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

async function smallTargets(page: Page): Promise<string[]> {
  return (await page.evaluate(`(() => {
    const out = []
    // A shadcn switch, checkbox or radio is drawn small and hit through a larger ::after
    // (after:-inset-x-3 after:-inset-y-2): its target is the box plus that overhang.
    const overhang = (el, side) => {
      const after = getComputedStyle(el, '::after')
      if (after.content === 'none' || after.position !== 'absolute') return 0
      return Math.max(0, -parseFloat(after[side]) || 0)
    }
    for (const el of document.querySelectorAll('button, input[type="checkbox"], input[type="radio"], summary')) {
      if (el.closest('[aria-hidden="true"], [hidden], nav')) continue
      const box = el.getBoundingClientRect()
      if (box.width === 0 || box.height === 0) continue
      if (getComputedStyle(el).visibility === 'hidden') continue
      const width = box.width + overhang(el, 'left') + overhang(el, 'right')
      const height = box.height + overhang(el, 'top') + overhang(el, 'bottom')
      if (width < 24 || height < 24) {
        const label = el.getAttribute('aria-label') || el.id || el.textContent.trim().slice(0, 40) || el.tagName
        out.push(el.tagName.toLowerCase() + ' ' + label + ' ' + Math.round(width) + 'x' + Math.round(height))
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
    // Open every fold, a plugin package's files (#1029) included, so what is inside one is
    // measured too. Each click takes one out of the closed set, so it is always the first.
    const closed = page.locator('main [data-slot="collapsible-trigger"][aria-expanded="false"]')
    while ((await closed.count()) > 0) await closed.first().click()
    await expect(page.getByRole('list', { name: 'Files to read in scadbuddy' })).toBeVisible()
    expect(await smallTargets(page)).toEqual([])
  })
}
