import { expect, test, type Page } from '@playwright/test'

// #2170 — Settings' Hotend usage: each rack hotend's serial, wear and loaded filament, and
// every spool that ran through it. msw-backed (`src/mocks/features/rack.ts`): printer 1
// has a rack, printer 2 has none.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

async function openHotendUsage(page: Page) {
  await page.goto('/settings#remembered')
  const rack = page.getByRole('region', { name: 'Hotend usage for 3DP-31B-598' })
  await rack.scrollIntoViewIfNeeded()
  await expect(rack).toBeVisible()
  return rack
}

for (const { name, viewport } of [
  { name: 'desktop', viewport: { width: 1280, height: 900 } },
  { name: 'phone', viewport: { width: 390, height: 844 } },
]) {
  test.describe(`at ${name} width`, () => {
    test.use({ viewport })

    test('each hotend shows its serial, wear, loaded filament and spool history', async ({ page }) => {
      const rack = await openHotendUsage(page)

      const used = rack.getByRole('listitem', { name: 'Position 2' })
      await expect(used).toContainText('Serial TEST-HOTEND-17')
      await expect(used).toContainText('12%')
      await expect(used.getByRole('img', { name: 'Bambu PLA Basic #3F8E43' })).toBeVisible()
      const spools = used.getByRole('list', { name: 'Spools run through position 2' })
      await expect(spools.getByRole('listitem')).toHaveText([/Bambu PLA Basic Mistletoe Green\s*×9 · 412 g/, /Inland PLA Black\s*×5 · 180 g/])

      // The mount that said "N/A": no serial, nothing loaded, no history.
      const empty = rack.getByRole('listitem', { name: 'Position 4' })
      await expect(empty).toContainText('No serial reported')
      await expect(empty).toContainText('Nothing loaded')
      // 128 is not a percentage.
      await expect(rack.getByRole('listitem', { name: 'Position 6' })).toContainText('not reported')

      // The printer with no rack is left out, and nothing alerts about it (#2102).
      await expect(page.getByRole('region', { name: 'Hotend usage for 3DP-77A-114' })).toHaveCount(0)
      await expect(page.getByText(/Hotend usage could not be read/)).toHaveCount(0)

      // Nothing runs off the side of the page.
      const overflow = await page.evaluate<number>(
        'document.documentElement.scrollWidth - document.documentElement.clientWidth',
      )
      expect(overflow).toBeLessThanOrEqual(0)

      await rack.screenshot({ path: test.info().outputPath(`hotend-usage-${name}.png`) })
      await page.screenshot({ path: test.info().outputPath(`hotend-usage-${name}-page.png`) })
    })
  })
}
