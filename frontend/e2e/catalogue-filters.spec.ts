import { expect, test } from '@playwright/test'
import { models } from '../src/mocks/fixtures'

// Every model the mocked catalogue lists, so a new fixture does not break the count.
const all = models.length

test.describe('catalogue filters (#276)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('a card tag chip filters, and the URL and count follow', async ({ page }) => {
    await page.goto('/')
    const count = page.getByTestId('result-count')
    await expect(count).toHaveText(`${all} of ${all}`)

    const coaster = page
      .getByRole('listitem')
      .filter({ has: page.getByRole('heading', { name: 'Crème Coaster' }) })
    await coaster.getByRole('button', { name: 'Filter by Tea & Coffee' }).click()

    await expect(page).toHaveURL(/\/\?tag=Tea\+%26\+Coffee&view=cards$/)
    await expect(count).toHaveText(`1 of ${all}`)
    await expect(page.getByRole('heading', { level: 2 })).toHaveText(['Crème Coaster'])

    // Back undoes the filter; forward and a reload restore it from the URL.
    await page.goBack()
    await expect(count).toHaveText(`${all} of ${all}`)
    await page.goForward()
    await expect(count).toHaveText(`1 of ${all}`)
    await page.reload()
    await expect(count).toHaveText(`1 of ${all}`)
    await expect(page.getByRole('button', { name: 'Tea & Coffee 1' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  test('"/" focuses the search, which matches without accents', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByTestId('result-count')).toHaveText(`${all} of ${all}`)

    await page.keyboard.press('/')
    const search = page.getByRole('searchbox', { name: 'Search models' })
    await expect(search).toBeFocused()
    await search.pressSequentially('creme')

    await expect(page).toHaveURL(/\/\?q=creme&view=cards$/)
    await expect(page.getByTestId('result-count')).toHaveText(`1 of ${all}`)
  })

  // #932 — at phone width the header actions did not wrap, so Add model sat past the
  // screen's edge, and every tag chip rendered before the first model (112 of them in
  // production, 120 Tab presses from the search box to the first card).
  test('at phone width Add model is reachable and the tags fold away', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/')
    await expect(page.getByTestId('result-count')).toHaveText(`${all} of ${all}`)

    const add = page.getByRole('button', { name: 'Add model' })
    const box = (await add.boundingBox())!
    expect(box.x + box.width, 'Add model ends on screen').toBeLessThanOrEqual(390)
    await add.click()
    await expect(page.getByRole('dialog')).toBeVisible()
    await page.keyboard.press('Escape')

    // The chips sit behind one disclosure, so the first card follows the filters.
    const toggle = page.getByRole('button', { name: /^Tags/ })
    await expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await expect(page.getByRole('button', { name: 'kitchen 1' })).toHaveCount(0)
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await page.getByRole('button', { name: 'kitchen 1' }).click()
    await expect(page.getByTestId('result-count')).toHaveText(`1 of ${all}`)
    // A selected tag stays in view with the list folded, so it can be cleared.
    await toggle.click()
    await expect(page.getByRole('button', { name: 'kitchen 1' })).toHaveAttribute('aria-pressed', 'true')
  })
})
