import { expect, test } from '@playwright/test'
import { models } from '../src/mocks/fixtures'

// Every model the mocked catalogue lists, so a new fixture does not break the count.
const all = models.length

test.describe('catalogue list mode (#278)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('the view lives in the URL: it survives a reload, and no view means Cards', async ({ page }) => {
    await page.goto('/')
    await expect(page).toHaveURL(/\/\?view=cards$/)
    await expect(page.getByTestId('result-count')).toHaveText(`${all} of ${all}`)
    const view = page.getByRole('group', { name: 'View' })
    await view.getByRole('button', { name: 'List' }).click()

    await expect(page).toHaveURL(/\/\?view=list$/)
    await expect(page.locator('[data-model-row]')).toHaveCount(all)

    await page.reload()
    await expect(page.locator('[data-model-row]')).toHaveCount(all)
    await expect(view.getByRole('button', { name: 'List' })).toHaveAttribute('aria-pressed', 'true')

    // A URL with no `view` shows Cards, whatever was chosen before.
    await page.goto('/')
    await expect(page).toHaveURL(/\/\?view=cards$/)
    await expect(page.locator('[data-model-row]')).toHaveCount(0)
    await expect(view.getByRole('button', { name: 'Cards' })).toHaveAttribute('aria-pressed', 'true')

    await view.getByRole('button', { name: 'List' }).click()
    await expect(page).toHaveURL(/\/\?view=list$/)

    // Back undoes the toggle, Forward redoes it.
    await page.goBack()
    await expect(page).toHaveURL(/\/\?view=cards$/)
    await expect(page.locator('[data-model-row]')).toHaveCount(0)
    await page.goForward()
    await expect(page).toHaveURL(/\/\?view=list$/)
    await expect(page.locator('[data-model-row]')).toHaveCount(all)
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
