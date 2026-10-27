import { expect, test, type Page } from '@playwright/test'

// #4873 — mid-rollout the HTML can name an entry script that the pod answering the asset
// request does not have (a real 404, #395). index.html's inline handler reloads once per
// failed URL. The preview serves PAGE_CSP, so a handler whose hash is missing from it is
// blocked and these fail.
test.skip(!!process.env.E2E_BASE_URL, 'routes the mocked build’s own entry')

/** The entry `<script type="module">` the built page names. */
async function entryUrl(page: Page): Promise<string> {
  const html = await (await page.request.get('/settings')).text()
  const src = /<script type="module" crossorigin src="([^"]+)"/.exec(html)?.[1]
  expect(src, 'the built page names an entry script').toBeTruthy()
  return src!
}

/** Counts the document loads of the page, the first one and every reload. */
function countDocuments(page: Page): () => number {
  let documents = 0
  page.on('request', (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documents++
  })
  return () => documents
}

test('a missing entry reloads the page once, and the reload renders it', async ({ page }) => {
  const entry = await entryUrl(page)
  let misses = 0
  await page.route(`**${entry}`, (route) => (misses++ === 0 ? route.fulfill({ status: 404, body: 'Not Found' }) : route.continue()))
  const documents = countDocuments(page)

  await page.goto('/settings', { waitUntil: 'commit' })
  await expect(page.locator('#root')).not.toBeEmpty()
  expect(documents()).toBe(2)
  expect(misses).toBe(2)
})

test('an entry that keeps 404ing reloads once and then stops', async ({ page }) => {
  const entry = await entryUrl(page)
  let misses = 0
  await page.route(`**${entry}`, (route) => {
    misses++
    return route.fulfill({ status: 404, body: 'Not Found' })
  })
  const documents = countDocuments(page)

  await page.goto('/settings', { waitUntil: 'commit' })
  await expect.poll(documents).toBe(2)
  await expect.poll(() => misses).toBe(2)
  // No third load: the URL that failed is remembered for this tab.
  await page.waitForTimeout(1500)
  expect(documents()).toBe(2)
  expect(misses).toBe(2)
  const failed = await page.evaluate(() => sessionStorage.getItem('scadbuddy:entry-reload'))
  expect(JSON.parse(failed ?? '[]')).toEqual([expect.stringMatching(new RegExp(`${entry.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`))])
})
