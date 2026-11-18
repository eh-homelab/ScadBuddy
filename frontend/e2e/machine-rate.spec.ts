import { expect, test } from '@playwright/test'

// #5774 (machine-rate actuals spec 2026-11-17 §9, §10) over the mocks
// (src/mocks/features/orders/machineRates.ts): log replacements on a printer with no counter
// and see the actual and its confidence change, at a desktop's and a phone's width.

test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

for (const [width, height] of [
  [1280, 900],
  [390, 844],
] as const) {
  test(`a logged replacement moves the actual and its confidence at ${width} px`, async ({ page }) => {
    await page.setViewportSize({ width, height })
    await page.goto('/settings/printing#orders')
    await page.getByRole('button', { name: 'Machine rate of Garage' }).click()
    const panel = page.getByRole('region', { name: 'Machine rate of Garage' })
    const inputs = panel.getByRole('form', { name: 'Machine rate inputs of Garage' })

    await inputs.getByLabel('Actuals window (events)').fill('2')
    await inputs.getByRole('button', { name: 'Add wear part' }).click()
    const wear = inputs.getByRole('group', { name: 'Wear part 1' })
    await wear.getByLabel('Part', { exact: true }).fill('Nozzle')
    await wear.getByLabel('Cost', { exact: true }).fill('35')
    await wear.getByLabel('Life (h)', { exact: true }).fill('1000')
    await inputs.getByRole('button', { name: 'Save machine rate' }).click()

    await expect(wear.getByTestId('figure-estimate')).toHaveText(/Estimate 0\.035\/h/)
    await expect(wear.getByTestId('figure-actual')).toHaveText('No events yet')
    await expect(wear.getByTestId('confidence')).toContainText('0 % confidence: 0 of 2 replacements logged')
    await wear.getByRole('radio', { name: 'Actual' }).click()
    await inputs.getByRole('button', { name: 'Save machine rate' }).click()
    await expect(wear.getByTestId('confidence')).toContainText('Estimate prices, 0 % confidence')

    // The printer has no counter: each reading is typed.
    for (const [reading, cost] of [
      ['100', '35'],
      ['700', '35'],
      ['1100', '40'],
    ]) {
      await wear.getByRole('button', { name: 'Log replacement' }).click()
      const form = wear.getByRole('form', { name: 'Log replacement of Nozzle' })
      await expect(form).toContainText('This printer has no counter')
      await form.getByLabel('Counter reading (h)').fill(reading)
      await form.getByLabel('Cost').fill(cost)
      await form.getByRole('button', { name: 'Log replacement' }).click()
      await expect(form).toBeHidden()
      if (reading === '700') {
        await expect(wear.getByTestId('figure-actual')).toHaveText(/Actual 0\.0583\/h/)
        await expect(wear.getByTestId('confidence')).toContainText('Estimate prices, 50 % confidence: 1 of 2 replacements logged')
        await expect(wear.getByTestId('figure-estimate')).toHaveAttribute('data-prices', 'true')
      }
    }

    // (35 + 40) over (600 + 400) h: the actual prices now.
    await expect(wear.getByTestId('figure-actual')).toHaveText(/Actual 0\.075\/h/)
    await expect(wear.getByTestId('figure-actual')).toHaveAttribute('data-prices', 'true')
    await expect(wear.getByTestId('confidence')).toContainText('100 % confidence: 2 of 2 replacements logged, 500 h each on average')
    await expect(panel.getByTestId('machine-rate-breakdown')).toContainText('Some parts priced from actuals; lowest confidence 100 %')

    await wear.getByRole('button', { name: 'Replacements (3)' }).click()
    const items = wear.getByRole('list', { name: 'Replacements: Nozzle' }).getByRole('listitem')
    await expect(items).toHaveCount(3)
    await expect(items.first()).toContainText('ran 400 h')
    await expect(items.first()).toContainText('logged here')

    // Nothing runs off the side of a phone.
    const overflow = (await page.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth')) as number
    expect(overflow).toBeLessThanOrEqual(0)
  })
}
