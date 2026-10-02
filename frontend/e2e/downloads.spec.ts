import { expect, test, type FrameLocator, type Page } from '@playwright/test'
import { bambuddyFrame } from './bambuddyFrame'

/**
 * Downloads, at the top level and inside Bambuddy's External Link frame. That frame is
 * sandboxed without `allow-downloads`, so Chromium silently drops a download started
 * in it; `lib/embed.ts` saves from a blank popup that escapes the sandbox instead.
 */
test.describe('downloads', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  async function generate(app: FrameLocator | Page) {
    await expect(app.getByTestId('bbox-readout')).toBeVisible({ timeout: 15_000 })
    await app.getByTestId('generate').click()
    await expect(app.getByText(/^Saved /)).toBeVisible()
  }

  test('downloads the 3MF at the top level', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await generate(page)
    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Download 3MF' }).click()
    expect((await download).suggestedFilename()).toMatch(/^name-keychain-[0-9a-f]+\.3mf$/)
  })

  test('downloads the 3MF inside Bambuddy’s sandboxed frame', async ({ page, baseURL }) => {
    const frame = await framed(page, baseURL)
    await generate(frame)

    const popup = page.context().waitForEvent('page')
    await frame.getByRole('button', { name: 'Download 3MF' }).click()
    const download = await (await popup).waitForEvent('download')
    expect(download.suggestedFilename()).toMatch(/^name-keychain-[0-9a-f]+\.3mf$/)
  })

  test('says to allow pop-ups when the frame’s popup is blocked', async ({ page, baseURL }) => {
    const frame = await framed(page, baseURL)
    await generate(frame)
    // A popup blocker: window.open answers null.
    const app = page.frames().find((candidate) => candidate.url().includes('/m/name-keychain'))
    if (!app) throw new Error('the ScadBuddy frame is not loaded')
    await app.evaluate('window.open = () => null')

    await frame.getByRole('button', { name: 'Download 3MF' }).click()
    await expect(frame.getByRole('alert')).toContainText('Allow pop-ups')
  })

  const framed = (page: Page, baseURL: string | undefined) => bambuddyFrame(page, baseURL, '/m/name-keychain')
})
