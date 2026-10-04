// frontend/e2e/tracing.spec.ts
import { expect, test, type Frame, type Page } from '@playwright/test'
import { bambuddyFrame } from './bambuddyFrame'

/**
 * #988, tracing spec 2026-10-01 §5.3 and §8: once the page's tracing has loaded (lazily,
 * after the first paint), `traceparent` goes on the page's own requests and never on a
 * request to another origin (Bambuddy, Google Fonts). The mocked relay answers "off"
 * (`src/mocks/features/telemetry.ts`), which makes the page undo its instrumentation, so
 * each test holds the relay at `200` while it checks `traceparent`: otherwise the first
 * export (about 5 s after the document-load span) could switch tracing off mid-assertion
 * and a cross-origin request would lack `traceparent` for that reason alone.
 */
test.describe('tracing', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')
  // The page CSP's `connect-src 'self'` stops a cross-origin fetch before it is sent,
  // so the test lifts it to see what the instrumentation would have put on one.
  test.use({ bypassCSP: true })

  const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/

  /** The `traceparent` that `where`'s own `fetch(url)` was sent with (undefined: none). */
  async function sentWith(page: Page, where: Page | Frame, url: string): Promise<string | undefined> {
    const target = new URL(url, where.url()).href
    const request = page.waitForRequest((r) => r.url() === target)
    await where.evaluate((u) => void fetch(u).catch(() => undefined), url)
    return (await request).headers()['traceparent']
  }

  /**
   * Holds every post to the relay unanswered, so no off signal arrives, until
   * `release()`; then they go on to the mocked relay. Call before the page loads.
   */
  async function holdRelay(page: Page): Promise<() => void> {
    let release: () => void = () => undefined
    const released = new Promise<void>((resolve) => (release = resolve))
    await page.route('**/telemetry/v1/traces', async (route) => {
      await released
      await route.fallback()
    })
    return release
  }

  test('puts traceparent on same-origin requests only', async ({ page }) => {
    await holdRelay(page)
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
    const release = await holdRelay(page)
    const relay = page.waitForRequest((r) => r.url() === `${origin}/telemetry/v1/traces`, { timeout: 20_000 })
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
    // Now let the relay answer as the mock does, off.
    release()
    // The document-load span's batch, flushed on the processor's schedule.
    const posted = await relay
    expect(posted.method()).toBe('POST')
    expect(posted.headers()['content-type']).toBe('application/json')
    expect((await posted.response())?.headers()['x-scadbuddy-tracing']).toBe('off')
  })
})
