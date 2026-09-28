import { expect, test, type Page } from '@playwright/test'

test.describe('catalogue card mode (#277)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test.beforeEach(async ({ page }) => {
    await page.goto('/')
  })

  function coaster(page: Page) {
    return page
      .getByRole('listitem')
      .filter({ has: page.getByRole('heading', { name: 'Crème Coaster' }) })
  }

  test('next changes the slide, stays on the catalogue and opens no lightbox', async ({
    page,
  }) => {
    const card = coaster(page)
    const position = card.getByTestId('carousel-position')
    await expect(position).toHaveText('1 of 4')

    await card.getByRole('button', { name: 'Next slide' }).click()

    await expect(position).toHaveText('2 of 4')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })

  test('rapid arrow clicks inside a card neither navigate nor open the lightbox', async ({
    page,
  }) => {
    const card = coaster(page)
    const next = card.getByRole('button', { name: 'Next slide' })
    const previous = card.getByRole('button', { name: 'Previous slide' })

    await next.click()
    await next.click({ delay: 0 })
    await next.click({ delay: 0 })
    await expect(card.getByTestId('carousel-position')).toHaveText('4 of 4')
    await previous.click({ delay: 0 })
    await previous.click({ delay: 0 })
    await expect(card.getByTestId('carousel-position')).toHaveText('2 of 4')

    // Enter on a focused arrow moves too, and opens nothing.
    await next.focus()
    await page.keyboard.press('Enter')
    await expect(card.getByTestId('carousel-position')).toHaveText('3 of 4')

    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })

  test('a click on the image opens the lightbox at it, and Esc closes it', async ({ page }) => {
    const card = coaster(page)
    await card.getByRole('button', { name: 'Next slide' }).click()
    await card.getByRole('button', { name: 'Open The raised rim' }).click()

    const lightbox = page.getByRole('dialog')
    await expect(lightbox).toBeVisible()
    await expect(lightbox).toContainText('The raised rim')
    await expect(page).toHaveURL(/\/$/)

    await page.keyboard.press('Escape')
    await expect(page.getByRole('dialog')).toHaveCount(0)
  })

  test('Enter on the focused media opens the lightbox', async ({ page }) => {
    const card = coaster(page)
    await card.getByRole('button', { name: 'Open Printed in blue and orange' }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByRole('dialog')).toContainText('Printed in blue and orange')
  })

  test('a click on the title opens the template', async ({ page }) => {
    await coaster(page).getByRole('link', { name: 'Crème Coaster' }).click()
    await expect(page).toHaveURL(/\/m\/creme-coaster$/)
  })

  test('a click on the card body outside the media opens the template', async ({ page }) => {
    // The title's stretched ::after is what takes the click there, which is why
    // Playwright's actionability check would refuse it: click the point itself.
    const box = await coaster(page).getByText('A drinks coaster with a raised rim.').boundingBox()
    if (!box) throw new Error('the description is not laid out')
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
    await expect(page).toHaveURL(/\/m\/creme-coaster$/)
  })
})
