import { expect, test } from '@playwright/test'
import { spoolId } from '../src/mocks/features/filament/seed'

// #6269 — a spool's history, the Metrics tab and the forecast view, against the msw API.
// The burn depends on today, so the clock is the day the backend's own `metrics` functions
// were run on the seed's plates (plan Decision 87).
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-11-18T12:00:00Z'))
})

test("a spool's page draws its grams with the anchors and lists what changed", async ({ page }) => {
  await page.goto(`/filament/spools/${spoolId(2)}`)
  await expect(page.getByRole('img', { name: 'Grams left: 1000 g to 820 g, 3 anchor readings' })).toBeVisible()
  const history = page.getByRole('list', { name: 'History of this spool' })
  const edited = history.getByRole('listitem').filter({ hasText: 'Edited' }).first()
  await expect(edited).toBeVisible()
  await expect(edited.getByRole('list', { name: 'Changes' })).toBeVisible()
})

test('the Metrics tab redraws its consumption when the grouping changes to Brand', async ({ page }) => {
  await page.goto('/filament/metrics')
  const consumption = page.getByRole('region', { name: 'Consumption' })
  await expect(consumption.getByRole('list', { name: 'Consumption by material' })).toBeVisible()
  await page.getByRole('combobox', { name: 'Group by' }).click()
  await page.getByRole('option', { name: 'By brand' }).click()
  await expect(consumption.getByRole('list', { name: 'Consumption by brand' })).toBeVisible()
  await expect(consumption.getByRole('list', { name: 'Consumption by material' })).toHaveCount(0)
})

test('the forecast view lists a type to reorder', async ({ page }) => {
  await page.goto('/filament/spools')
  await page.getByRole('button', { name: 'Forecast' }).click()
  const types = page.getByRole('table', { name: 'Days of stock by type' })
  const first = types.getByRole('row').nth(1)
  await expect(first).toContainText('Reorder now')
})

test('the forecast badge follows the table: under the type name exactly when the table scrolls sideways (R-T8-12)', async ({ page }) => {
  // 680 px is wider than `sm`, yet the app shell leaves the table's container under its 44 rem, so it scrolls.
  for (const width of [390, 680, 1600]) {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/filament/spools?view=forecast')
    const table = page.getByRole('table', { name: 'Days of stock by type' })
    await expect(table).toBeVisible()
    const scrolls = await table.evaluate((el) => el.parentElement!.scrollWidth > el.parentElement!.clientWidth + 1)
    const cells = table.getByRole('row').nth(1).getByRole('cell')
    const underName = cells.first().getByText('Reorder now')
    const inReorderBy = cells.last().getByText('Reorder now')
    const date = cells.last().getByText(/^[A-Z][a-z]{2} \d{1,2}, \d{4}$/)
    // Scrolling: the badge is under the name and "Reorder by" shows the day. Not scrolling: the badge is in "Reorder by".
    if (scrolls) {
      await expect(underName).toBeVisible()
      await expect(inReorderBy).toBeHidden()
      await expect(date).toBeVisible()
    } else {
      await expect(underName).toBeHidden()
      await expect(inReorderBy).toBeVisible()
      await expect(date).toBeHidden()
    }
    if (width === 680) expect(scrolls).toBe(true)
  }
})

test('the Spools toolbar wraps at a phone width, so Export CSV is fully in view (R-T8-12)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 })
  await page.goto('/filament/spools')
  const button = page.getByRole('button', { name: 'Export CSV' })
  await expect(button).toBeVisible()
  const box = (await button.boundingBox())!
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(390)
  if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/spools-390.png`, fullPage: true })
})
