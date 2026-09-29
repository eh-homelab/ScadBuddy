import { expect, test, type Page } from '@playwright/test'

function prints(page: Page) {
  return page.getByRole('list', { name: 'Prints' }).locator('[data-print]')
}

test.describe('print history (#310)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('the nav opens the history, and a status filter follows the URL', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Prints' }).click()
    await expect(page).toHaveURL(/\/prints$/)
    await expect(prints(page)).toHaveCount(4)
    await expect(page.locator('[data-print="38"]')).toContainText('Deleted in Bambuddy')

    await page.getByLabel('Status').selectOption('failed')
    await expect(page).toHaveURL(/\/prints\?status=failed$/)
    await expect(prints(page)).toHaveCount(1)
    await expect(page.locator('[data-print="36"]')).toContainText('Failed')

    // Back undoes the filter; a reload restores it from the URL.
    await page.goBack()
    await expect(prints(page)).toHaveCount(4)
    await page.goForward()
    await page.reload()
    await expect(prints(page)).toHaveCount(1)
    await expect(page.getByLabel('Status')).toHaveValue('failed')
  })

  test('text search and Clear filters', async ({ page }) => {
    await page.goto('/prints')
    await expect(prints(page)).toHaveCount(4)
    await page.getByRole('searchbox', { name: 'Search prints' }).pressSequentially('nova')
    await expect(page).toHaveURL(/\/prints\?q=nova$/)
    await expect(prints(page)).toHaveCount(1)
    await page.getByRole('button', { name: 'Clear filters' }).first().click()
    await expect(page).toHaveURL(/\/prints$/)
    await expect(prints(page)).toHaveCount(4)
  })

  test('the image opens the lightbox with the print’s media, and Esc closes it', async ({ page }) => {
    await page.goto('/prints')
    await page.locator('[data-print="35"]').getByRole('button', { name: /^Open media of/ }).click()
    const lightbox = page.getByRole('dialog')
    await expect(lightbox).toBeVisible()
    await expect(lightbox).toContainText('Timelapse')
    await expect(page).toHaveURL(/\/prints$/)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })

  test('a click on the row opens the print, and stays there', async ({ page }) => {
    await page.goto('/prints')
    // Low on the card, away from the name and the image: the whole item is the link.
    const item = page.locator('[data-print="36"]')
    const box = await item.boundingBox()
    await item.click({ position: { x: 24, y: (box?.height ?? 0) - 12 } })
    // Settled on the print, not bounced back to the catalogue by the catch-all route.
    await expect(page.getByRole('heading', { name: 'Reagan', level: 1 })).toBeVisible()
    await expect(page).toHaveURL(/\/prints\/36$/)
    await page.getByRole('main').getByRole('link', { name: 'Prints', exact: true }).click()
    await expect(page).toHaveURL(/\/prints$/)
  })

  test('the template’s Prints tab lists its prints, in the view last chosen', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await page.getByRole('main').getByRole('link', { name: 'Prints', exact: true }).click()
    await expect(page).toHaveURL(/\/m\/name-keychain\/prints$/)
    await expect(page.getByRole('heading', { name: 'Prints' })).toBeVisible()
    await expect(prints(page)).toHaveCount(4)
    await expect(page.getByLabel('Template')).toHaveCount(0)

    await page.getByRole('group', { name: 'View' }).getByRole('button', { name: 'List' }).click()
    await expect(page).toHaveURL(/\/m\/name-keychain\/prints\?view=list$/)
    await page.goto('/m/name-keychain/prints')
    await expect(page.getByRole('list', { name: 'Prints' })).toHaveAttribute('data-view', 'list')
  })
})
