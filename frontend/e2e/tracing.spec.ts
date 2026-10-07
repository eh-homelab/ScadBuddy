// frontend/e2e/tracing.spec.ts
import { expect, test, type Frame, type Page } from '@playwright/test'
import { bambuddyFrame } from './bambuddyFrame'

/**
 * #988, tracing spec 2026-10-01 §5.3 and §8: once the page's tracing has loaded (lazily,
 * after the first paint), `traceparent` goes on the page's own requests and never on a
 * request to another origin (Bambuddy, Google Fonts). The mocked relay answers "off"
 * (`src/mocks/features/telemetry.ts`), which makes the page undo its instrumentation, so
 * each test answers the relay itself, tracing on (`answerRelay`): otherwise the first
 * export (about 5 s after the document-load span) could switch tracing off mid-assertion
 * and a cross-origin request would lack `traceparent` for that reason alone.
 */
test.describe('tracing', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')
  // The page CSP's `connect-src 'self'` stops a cross-origin fetch before it is sent,
  // so the test lifts it to see what the instrumentation would have put on one.
  test.use({ bypassCSP: true })

  /** A `fetch` outside any action is a parentless CLIENT span: propagated, unsampled (spec §6). */
  const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-00$/

  interface RelayPost {
    url: string
    method: string
    contentType: string | null
  }

  /** The `traceparent` that `where`'s own `fetch(url)` was sent with (undefined: none). */
  async function sentWith(page: Page, where: Page | Frame, url: string): Promise<string | undefined> {
    const target = new URL(url, where.url()).href
    const request = page.waitForRequest((r) => r.url() === target)
    await where.evaluate((u) => void fetch(u).catch(() => undefined), url)
    return (await request).headers()['traceparent']
  }

  /**
   * Answers every post to the relay in the page itself, at once, with a `204` and tracing
   * on, and records it in `__relayPosts`. Not `page.route`: the msw service worker answers
   * the relay (off) before a route sees it. Not left unanswered either: `RelayExporter`
   * aborts a request after 10 s. Call before the page loads; it covers every frame.
   */
  async function answerRelay(page: Page): Promise<void> {
    await page.addInitScript((path) => {
      const posts: RelayPost[] = []
      ;(globalThis as unknown as { __relayPosts: RelayPost[] }).__relayPosts = posts
      const original = globalThis.fetch
      // The page's `location`; this file is typed for Node, which has none.
      const base = (globalThis as unknown as { location: URL }).location.href
      globalThis.fetch = (input, init) => {
        const url = new URL(input instanceof Request ? input.url : String(input), base)
        if (url.pathname !== path) return original(input, init)
        posts.push({
          url: url.href,
          method: init?.method ?? (input instanceof Request ? input.method : 'GET'),
          contentType: new Headers(init?.headers).get('content-type'),
        })
        return Promise.resolve(new Response(null, { status: 204 }))
      }
    }, '/telemetry/v1/traces')
  }

  function relayPosts(where: Page | Frame): Promise<RelayPost[]> {
    return where.evaluate(() => (globalThis as unknown as { __relayPosts: RelayPost[] }).__relayPosts)
  }

  test('puts traceparent on same-origin requests only', async ({ page }) => {
    await answerRelay(page)
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Models' })).toBeVisible()

    await expect.poll(() => sentWith(page, page, '/api/v1/models'), { timeout: 15_000 }).toMatch(TRACEPARENT)
    await page.route('https://fonts.googleapis.com/**', (route) => route.fulfill({ status: 200, body: '' }))
    expect(await sentWith(page, page, 'https://fonts.googleapis.com/css2?family=Lobster+Two')).toBeUndefined()
    // And again on the page's own origin, so the absence above is the origin's doing.
    expect(await sentWith(page, page, '/api/v1/models')).toMatch(TRACEPARENT)
  })

  test("traces inside Bambuddy's sandboxed frame and exports to ScadBuddy's own origin", async ({ page, baseURL }) => {
    const origin = new URL('/', baseURL).origin
    await answerRelay(page)
    const frame = await bambuddyFrame(page, baseURL, '/')
    await expect(frame.getByRole('heading', { name: 'Models' })).toBeVisible()
    const app = page.frames().find((f) => f.url().startsWith(origin))
    if (!app) throw new Error('the ScadBuddy frame is missing')

    await expect.poll(() => sentWith(page, app, '/api/v1/models'), { timeout: 15_000 }).toMatch(TRACEPARENT)
    // The origin check holds inside the frame too: a cross-origin fetch carries none.
    await page.route('https://other-origin.test/**', (route) => route.fulfill({ status: 200, body: '' }))
    expect(await sentWith(page, app, 'https://other-origin.test/x')).toBeUndefined()
    // And again on the frame's own origin, so the absence above is the origin's doing.
    expect(await sentWith(page, app, '/api/v1/models')).toMatch(TRACEPARENT)
    // The document-load span's batch, flushed on the processor's schedule.
    await expect.poll(() => relayPosts(app).then((posts) => posts.length), { timeout: 20_000 }).toBeGreaterThan(0)
    const [posted] = await relayPosts(app)
    expect(posted).toEqual({ url: `${origin}/telemetry/v1/traces`, method: 'POST', contentType: 'application/json' })
  })
})
