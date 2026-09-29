import { expect, test } from '@playwright/test'

test.describe('catalogue filters (#276)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('a card tag chip filters, and the URL and count follow', async ({ page }) => {
    await page.goto('/')
    const count = page.getByTestId('result-count')
    await expect(count).toHaveText('4 of 4')

    const coaster = page
      .getByRole('listitem')
      .filter({ has: page.getByRole('heading', { name: 'Crème Coaster' }) })
    await coaster.getByRole('button', { name: 'Filter by Tea & Coffee' }).click()

    await expect(page).toHaveURL(/\/\?tag=Tea\+%26\+Coffee&view=cards$/)
    await expect(count).toHaveText('1 of 4')
    await expect(page.getByRole('heading', { level: 2 })).toHaveText(['Crème Coaster'])

    // Back undoes the filter; forward and a reload restore it from the URL.
    await page.goBack()
    await expect(count).toHaveText('4 of 4')
    await page.goForward()
    await expect(count).toHaveText('1 of 4')
    await page.reload()
    await expect(count).toHaveText('1 of 4')
    await expect(page.getByRole('button', { name: 'Tea & Coffee 1' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  test('"/" focuses the search, which matches without accents', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByTestId('result-count')).toHaveText('4 of 4')

    await page.keyboard.press('/')
    const search = page.getByRole('searchbox', { name: 'Search models' })
    await expect(search).toBeFocused()
    await search.pressSequentially('creme')

    await expect(page).toHaveURL(/\/\?q=creme&view=cards$/)
    await expect(page.getByTestId('result-count')).toHaveText('1 of 4')
  })
})
