import { expect, test } from '@playwright/test'

// #322 — a settings round-trip against the msw API: a value saved in one section
// keeps its source across navigation, a reset puts the deployment's value back, and
// unsaved edits hold an in-app link until the user decides.

test.describe('settings', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('saves a section, keeps it across navigation, and resets it to the deployment', async ({ page }) => {
    await page.goto('/settings')
    const rendering = page.getByRole('region', { name: 'Rendering' })
    const timeout = page.getByLabel('Render timeout')
    const source = page.getByTestId('source-render_timeout')
    await expect(timeout).toHaveValue('300')
    await expect(source).toHaveText(/From SCADBUDDY_RENDER_TIMEOUT/)

    await timeout.fill('45')
    await expect(rendering.getByText('Unsaved')).toBeVisible()
    await rendering.getByRole('button', { name: 'Save Rendering' }).click()
    await expect(rendering.getByText('Unsaved')).toHaveCount(0)
    await expect(source).toHaveText(/Set here/)

    // Away and back inside the app: the mock backend holds what was saved.
    await page.getByRole('link', { name: 'Models', exact: true }).click()
    await expect(page).toHaveURL(/\/$/)
    await page.getByRole('link', { name: 'Settings', exact: true }).click()
    await expect(timeout).toHaveValue('45')
    await expect(source).toHaveText(/Set here/)

    await source.getByRole('button', { name: /Reset render_timeout/ }).click()
    await expect(source).toHaveText(/From SCADBUDDY_RENDER_TIMEOUT/)
    await expect(timeout).toHaveValue('300')
  })

  test('holds an in-app link while a section is unsaved, and discards on request', async ({ page }) => {
    await page.goto('/settings')
    const preview = page.getByRole('region', { name: 'Preview' })
    await page.getByLabel('Show dimensions in').selectOption('in')
    await expect(preview.getByText('Unsaved')).toBeVisible()

    await page.getByRole('link', { name: 'Models', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Leave without saving?' })
    await expect(dialog).toContainText('Preview')
    await dialog.getByRole('button', { name: 'Stay' }).click()
    await expect(page).toHaveURL(/\/settings$/)

    await preview.getByRole('button', { name: 'Discard Preview changes' }).click()
    await expect(page.getByLabel('Show dimensions in')).toHaveValue('mm')
    await page.getByRole('link', { name: 'Models', exact: true }).click()
    await expect(dialog).toHaveCount(0)
    await expect(page).toHaveURL(/\/$/)
  })
})
