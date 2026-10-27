import { expect, test, type Page } from '@playwright/test'

// #4873 — mid-rollout the HTML can name an entry script that the pod answering the asset
// request does not have (a real 404, #395). index.html's inline handler reloads, at most
// 3 times per failed URL, and only for the tags parsed from the HTML (#4874). The preview serves PAGE_CSP, so a handler whose hash is missing from it is
// blocked and these fail.
test.skip(!!process.env.E2E_BASE_URL, 'routes the mocked build’s own entry')

/** The entry `<script type="module">` the built page names. */
async function entryUrl(page: Page): Promise<string> {
  const html = await (await page.request.get('/settings')).text()
  const src = /<script type="module" crossorigin src="([^"]+)"/.exec(html)?.[1]
  expect(src, 'the built page names an entry script').toBeTruthy()
  return src!
}

/** The entry stylesheet the built page names. */
async function entryCss(page: Page): Promise<string> {
  const html = await (await page.request.get('/settings')).text()
  const href = /<link rel="stylesheet" crossorigin href="([^"]+)"/.exec(html)?.[1]
  expect(href, 'the built page names an entry stylesheet').toBeTruthy()
  return href!
}

function guard(page: Page, key: string): Promise<string | null> {
  return page.evaluate((k) => sessionStorage.getItem(k), key)
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

test('an entry that keeps 404ing reloads 3 times and then stops', async ({ page }) => {
  const entry = await entryUrl(page)
  let misses = 0
  await page.route(`**${entry}`, (route) => {
    misses++
    return route.fulfill({ status: 404, body: 'Not Found' })
  })
  const documents = countDocuments(page)

  await page.goto('/settings', { waitUntil: 'commit' })
  // The first load and 3 reloads, 0, 0.5 and 1 s apart.
  await expect.poll(documents, { timeout: 10_000 }).toBe(4)
  await expect.poll(() => misses).toBe(4)
  // No fifth load: the URL has used its reloads for this tab.
  await page.waitForTimeout(3000)
  expect(documents()).toBe(4)
  expect(misses).toBe(4)
  const failed = JSON.parse((await guard(page, 'scadbuddy:entry-reload')) ?? '[]') as string[]
  expect(failed).toHaveLength(3)
  expect(failed.every((url) => url.endsWith(entry))).toBe(true)
})

test('a missing entry stylesheet reloads the page', async ({ page }) => {
  const css = await entryCss(page)
  let misses = 0
  await page.route(`**${css}`, (route) => (misses++ === 0 ? route.fulfill({ status: 404, body: 'Not Found' }) : route.continue()))
  const documents = countDocuments(page)

  await page.goto('/settings', { waitUntil: 'commit' })
  await expect(page.locator('#root')).not.toBeEmpty()
  await expect.poll(documents).toBe(2)
  expect(misses).toBe(2)
})

test("a lazy page's missing CSS is left to the stale-chunk reload", async ({ page, context }) => {
  // FlowPage's chunk preloads the editor's CSS (vite's preload helper adds the <link>,
  // long after DOMContentLoaded). staleChunks.ts reloads once for the build; this handler
  // must not act on it, or its per-URL guard would decide instead. `context.route`: by
  // then msw's service worker fetches the page's assets, which `page.route` never sees.
  let misses = 0
  await context.route(/\/assets\/editor-[^/]+\.css$/, (route) => {
    misses++
    return route.fulfill({ status: 404, body: 'Not Found' })
  })
  const documents = countDocuments(page)

  await page.goto('/workflows/flows/def-swap', { waitUntil: 'commit' })
  await expect.poll(documents).toBe(2)
  await expect.poll(() => misses).toBe(2)
  await page.waitForTimeout(2000)
  expect(documents()).toBe(2)
  expect(await guard(page, 'scadbuddy:stale-chunk-reload')).toMatch(/\/assets\/index-[^/]+\.js$/)
  expect(await guard(page, 'scadbuddy:entry-reload')).toBeNull()
})
