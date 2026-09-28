import { expect, test } from '@playwright/test'

test.describe('template page gallery (#280)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; drives the fixtures’ coaster')

  test('shows the media under the preview on a wide screen and opens it in the lightbox', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.goto('/m/creme-coaster')

    await expect(page.getByTestId('preview-canvas')).toBeVisible()
    const strip = page.getByRole('list', { name: 'Gallery' })
    await expect(strip.getByRole('button')).toHaveCount(4)
    // Wide screens have the strip, not the tabs.
    await expect(page.getByRole('tablist', { name: 'View' })).toBeHidden()

    await strip.getByRole('button', { name: 'Open Printing on an H2C' }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expect(dialog.locator('video')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()
    await expect(page.getByTestId('preview-canvas')).toBeVisible()
  })

  test('puts the gallery on a tab beside the preview on a narrow screen', async ({ page }) => {
    await page.setViewportSize({ width: 420, height: 860 })
    await page.goto('/m/creme-coaster')

    await expect(page.getByTestId('preview-canvas')).toBeVisible()
    await expect(page.getByRole('list', { name: 'Gallery' })).toBeHidden()

    await page.getByRole('tab', { name: /Gallery/ }).click()
    const carousel = page.getByRole('region', { name: 'Crème Coaster' })
    await expect(carousel).toBeVisible()
    await expect(page.getByTestId('preview-canvas')).toBeHidden()
    await carousel.getByRole('button', { name: 'Open Printed in blue and orange' }).click()
    await expect(page.getByRole('dialog')).toBeVisible()
    await page.keyboard.press('Escape')

    await page.getByRole('tab', { name: 'Preview' }).click()
    await expect(page.getByTestId('preview-canvas')).toBeVisible()
  })

  test('has no gallery for a template with no media', async ({ page }) => {
    await page.goto('/m/gridfinity-bin')

    await expect(page.getByTestId('preview-canvas')).toBeVisible()
    await expect(page.getByRole('list', { name: 'Gallery' })).toHaveCount(0)
    await expect(page.getByRole('tab', { name: /Gallery/ })).toHaveCount(0)
  })
})
