import { expect, test, type Page } from '@playwright/test'

// #5282 (spec 2026-11-02 §5.3), #6157 — the back office's Storefront page over the mocks
// (src/mocks/features/storefront.ts). One test makes and publishes listings by hand; the
// other starts from 2,000 listings, as a shop with thousands would: it searches, pages,
// publishes in bulk, moves one to the top, and edits one and comes back to the same page.

const SEEDED = 2000
const STORAGE_KEY = 'sb-mock-storefront-listings'

/** 2,000 listings put where the mock reads them at start, once per test (a reload keeps the mock's own). */
async function seedListings(page: Page) {
  await page.addInitScript(
    ({ count, key }) => {
      if (sessionStorage.getItem(key)) return
      const kinds = ['Name Keychain', 'Pencil Cup', 'Coaster', 'Tag']
      const listings = Array.from({ length: count }, (_, i) => {
        const at = new Date(Date.UTC(2026, 10, 1) + i * 60_000).toISOString()
        return {
          id: `seed-${String(i).padStart(4, '0')}`,
          slug: 'name-keychain',
          commit: 'c3d4e5f6a7b8'.repeat(4).slice(0, 40),
          title: `${kinds[i % kinds.length]} ${i + 1}`,
          description: '',
          position: i,
          published: false,
          params: {},
          fixed: {},
          colours: [],
          media_ids: [],
          snapshot: null,
          media: [],
          published_at: null,
          created_at: at,
          updated_at: at,
        }
      })
      sessionStorage.setItem(key, JSON.stringify(listings))
    },
    { count: SEEDED, key: STORAGE_KEY },
  )
}

const rows = (page: Page) => page.getByTestId('storefront-listing')
const rowTitles = (page: Page) => rows(page).getByRole('link')

test.describe('storefront listings', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack has no storefront listings yet')

  test('makes and publishes a listing, and it is listed', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Storefront' }).click()
    await expect(page.getByRole('heading', { name: 'Storefront', level: 1 })).toBeVisible()
    await expect(page.getByText(/No listings yet/)).toBeVisible()

    await page.getByRole('link', { name: 'New listing' }).click()
    await expect(page).toHaveURL(/\/storefront\/listings\/new$/)
    const titleField = page.getByLabel('Title', { exact: true })
    // The new listing takes the first template's name once the templates load.
    await expect(titleField).toHaveValue('Name Keychain')
    await titleField.fill('Keychain')
    // Its parameters load from the schema before it can be published as offered.
    await expect(page.getByTestId('listing-preview').getByLabel('Name on the tag')).toBeVisible()
    await page.getByTestId('listing-actions').getByRole('button', { name: 'Publish', exact: true }).click()
    await expect(page.getByTestId('storefront-listing-page').getByText('Published', { exact: true })).toBeVisible()
    // The address is the listing's own now: a reload opens it again.
    await expect(page).toHaveURL(/\/storefront\/listings\/[0-9a-f-]{36}$/)
    await page.reload()
    await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Keychain')

    await page.getByRole('link', { name: 'Back to listings' }).click()
    await expect(rowTitles(page)).toHaveText(['Keychain'])
    await expect(rows(page).first()).toContainText('Published')
  })

  test('searches, pages, publishes in bulk, moves and edits among 2,000 listings', async ({ page }) => {
    await seedListings(page)
    await page.goto('/storefront')
    await expect(page.getByTestId('storefront-range')).toHaveText('1–50 of 2,000')
    await expect(rows(page)).toHaveCount(50)

    // Page 2, kept in the address.
    await page.getByRole('button', { name: 'Next' }).click()
    await expect(page).toHaveURL(/[?&]page=2/)
    await expect(rowTitles(page).first()).toHaveText('Coaster 51')

    // The server's search. Every fourth listing is a coaster, numbered 3, 7, 11, …; those
    // whose number starts with 1 are 11, 15, 19, 103 … 199 and 1003 … 1999: 3 + 25 + 250.
    await page.getByRole('searchbox', { name: 'Search title or model' }).fill('coaster 1')
    await expect(page).toHaveURL(/[?&]q=coaster\+1/)
    await expect(page.getByTestId('storefront-range')).toHaveText('1–50 of 278')
    await expect(rowTitles(page).first()).toHaveText('Coaster 11')
    // A new search starts again at page 1.
    await expect(page).not.toHaveURL(/page=/)

    // Bulk publish the page's first three.
    for (const title of ['Coaster 11', 'Coaster 15', 'Coaster 19']) {
      await page.getByRole('checkbox', { name: `Select ${title}`, exact: true }).check()
    }
    await page.getByRole('region', { name: 'Selected listings' }).getByRole('button', { name: 'Publish', exact: true }).click()
    await expect(page.getByText('Published 3 of 3.')).toBeVisible()
    await page.getByRole('combobox', { name: 'Status' }).click()
    await page.getByRole('option', { name: 'Published' }).click()
    await expect(page.getByTestId('storefront-range')).toHaveText('1–3 of 3')
    await expect(rowTitles(page)).toHaveText(['Coaster 11', 'Coaster 15', 'Coaster 19'])

    // Move one to the top of the whole shop, with one request.
    await page.getByRole('button', { name: 'Actions for Coaster 19' }).click()
    await page.getByRole('menuitem', { name: 'Move to top' }).click()
    await expect(rowTitles(page)).toHaveText(['Coaster 19', 'Coaster 11', 'Coaster 15'])
    await expect(rows(page).first().getByRole('cell').nth(5)).toHaveText('1')
    // Sort by title, so the list's address holds a search, a filter and a sort.
    await page.getByRole('button', { name: 'Title' }).click()
    await expect(rowTitles(page)).toHaveText(['Coaster 11', 'Coaster 15', 'Coaster 19'])
    await expect(page).toHaveURL(/[?&]sort=title/)
    const listUrl = page.url()

    // Edit one, save, and come back to the same search, filter and sort.
    await rowTitles(page).filter({ hasText: 'Coaster 15' }).click()
    await expect(page).toHaveURL(/\/storefront\/listings\/seed-0014$/)
    await page.getByLabel('Title', { exact: true }).fill('Coaster fifteen 1')
    await expect(page.getByText('Unsaved changes')).toBeVisible()
    await page.getByTestId('listing-actions').getByRole('button', { name: 'Save draft' }).click()
    // Saved: the editor stays, and says nothing is left to save.
    await expect(page.getByText('Unsaved changes')).toHaveCount(0)
    await expect(page).toHaveURL(/\/storefront\/listings\/seed-0014$/)
    await page.getByRole('link', { name: 'Back to listings' }).click()
    await expect(page).toHaveURL(listUrl)
    // Saving a published listing takes it off the store, so the "Published" filter now lists two.
    await expect(rowTitles(page)).toHaveText(['Coaster 11', 'Coaster 19'])
  })
})
