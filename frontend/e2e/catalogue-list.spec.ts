import { expect, test } from '@playwright/test'

test.describe('catalogue list mode (#278)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('the List view survives a reload, and is remembered without the URL', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByTestId('result-count')).toHaveText('4 of 4')
    const view = page.getByRole('group', { name: 'View' })
    await view.getByRole('button', { name: 'List' }).click()

    await expect(page).toHaveURL(/\/\?view=list$/)
    await expect(page.locator('[data-model-row]')).toHaveCount(4)

    await page.reload()
    await expect(page.locator('[data-model-row]')).toHaveCount(4)
    await expect(view.getByRole('button', { name: 'List' })).toHaveAttribute('aria-pressed', 'true')

    // With no `view` in the URL, the browser's last choice is restored into it.
    await page.goto('/')
    await expect(page).toHaveURL(/\/\?view=list$/)
    await expect(page.locator('[data-model-row]')).toHaveCount(4)

    await view.getByRole('button', { name: 'Cards' }).click()
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('[data-model-row]')).toHaveCount(0)

    // Back undoes the toggle, Forward redoes it.
    await page.goBack()
    await expect(page).toHaveURL(/\/\?view=list$/)
    await expect(page.locator('[data-model-row]')).toHaveCount(4)
    await page.goForward()
    await expect(page).toHaveURL(/\/$/)
    await expect(page.locator('[data-model-row]')).toHaveCount(0)
    await page.reload()
    await expect(view.getByRole('button', { name: 'Cards' })).toHaveAttribute('aria-pressed', 'true')
  })

  test('a thumbnail opens the lightbox and the name navigates', async ({ page }) => {
    await page.goto('/?view=list')
    const coaster = page
      .locator('[data-model-row]')
      .filter({ has: page.getByRole('heading', { name: 'Crème Coaster' }) })

    await coaster.getByRole('button', { name: /^View media of Crème Coaster/ }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText('Printed in blue and orange')
    await expect(page).toHaveURL(/\/\?view=list$/)
    await page.keyboard.press('Escape')
    await expect(dialog).toBeHidden()

    await coaster.getByRole('link', { name: 'Crème Coaster' }).click()
    await expect(page).toHaveURL(/\/m\/creme-coaster$/)
  })
})
