import { expect, test } from '@playwright/test'

// #4698 — "Import print": a Bambuddy archive ScadBuddy did not print, linked to a model
// from the Prints page, then listed on that model's Prints tab.
test.describe('import print (#4698)', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('links an unlinked archive to a model, which then lists it', async ({ page }) => {
    await page.goto('/prints')
    await page.getByTestId('import-print').click()
    const dialog = page.getByRole('dialog', { name: 'Import print' })
    await dialog.getByTestId('past-9105').click()
    const sources = dialog.getByRole('radiogroup', { name: 'Source' })
    await expect(sources.getByRole('radio', { name: /The archive's file/ })).toBeChecked()
    await dialog.getByRole('combobox', { name: 'Model to link' }).click()
    await page.getByRole('option', { name: 'Name Keychain' }).click()
    await dialog.getByRole('button', { name: 'Import' }).click()
    await expect(dialog.getByTestId('import-done')).toContainText('name-keychain')

    await dialog.getByRole('link', { name: "The model's prints" }).click()
    await expect(page).toHaveURL(/\/m\/name-keychain\/prints$/)
    await expect(page.locator('[data-print="9105"]')).toBeVisible()
  })
})
