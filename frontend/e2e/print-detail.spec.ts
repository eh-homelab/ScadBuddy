import { expect, test, type FrameLocator, type Page } from '@playwright/test'
import { bambuddyFrame } from './bambuddyFrame'

/**
 * #311 — one print's page: its gallery and timelapse through the media proxy, its
 * files, provenance and outcome, and its actions, also inside Bambuddy's sandboxed
 * External Link frame.
 */
test.describe('print detail', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

  test('shows the print and plays its timelapse through the Range proxy', async ({ page }) => {
    const timelapse = page.waitForRequest((request) => request.url().endsWith('/api/v1/prints/35/timelapse'))
    await page.goto('/prints/35')

    await expect(page.getByRole('heading', { name: 'Reagan', level: 1 })).toBeVisible()
    const outcome = page.getByRole('region', { name: 'Outcome' })
    await expect(outcome).toContainText('3DP-31B-598')
    await expect(outcome.getByRole('list', { name: 'Runs' }).getByRole('listitem')).toHaveCount(2)
    await expect(page.getByRole('region', { name: 'Provenance' }).getByRole('table', { name: 'Parameters' })).toContainText(
      'Reagan',
    )
    await expect(page.getByTestId('preview-canvas')).toBeVisible()

    // A native <video> against the proxy: the browser asks for byte ranges.
    const video = page.getByRole('region', { name: 'Timelapse' }).locator('video')
    await expect(video).toHaveAttribute('src', '/api/v1/prints/35/timelapse')
    expect((await timelapse).headers()['range']).toMatch(/^bytes=\d+-/)

    const gallery = page.getByRole('region', { name: 'Gallery' })
    await expect(gallery.getByTestId('carousel-position')).toHaveText('1 of 3')
    await gallery.getByRole('button', { name: 'Open Finish photo' }).click()
    const lightbox = page.getByRole('dialog')
    await expect(lightbox).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(lightbox).toBeHidden()
  })

  test('downloads a file and the parameters', async ({ page }) => {
    await page.goto('/prints/35')
    const files = page.getByRole('region', { name: 'Files' })

    const sliced = page.waitForEvent('download')
    await files.getByRole('button', { name: 'Download name-keychain-reagan.gcode.3mf' }).click()
    expect((await sliced).suggestedFilename()).toBe('name-keychain-reagan.gcode.3mf')

    const params = page.waitForEvent('download')
    await files.getByRole('button', { name: 'Download parameters as JSON' }).click()
    const saved = await params
    expect(saved.suggestedFilename()).toBe('name-keychain-reagan-params.json')
    const body = await saved.createReadStream()
    const chunks: Buffer[] = []
    for await (const chunk of body) chunks.push(chunk as Buffer)
    expect(JSON.parse(Buffer.concat(chunks).toString('utf8'))).toMatchObject({ name: 'Reagan' })
  })

  test('prints again behind a confirmation', async ({ page }) => {
    await page.goto('/prints/35')
    await page.getByRole('button', { name: 'Print again' }).click()
    const dialog = page.getByRole('dialog', { name: 'Print again' })
    await expect(dialog).toContainText('3DP-31B-598')
    await dialog.getByRole('button', { name: 'Queue' }).click()
    await expect(dialog).toContainText('Queued as #200')
  })

  test('customizes from the print', async ({ page }) => {
    await page.goto('/prints/35')
    await page.getByRole('link', { name: 'Customize from this' }).click()
    await expect(page).toHaveURL(/\/m\/name-keychain\?from=a{32}/)
  })

  test('shows what ScadBuddy still has of a print deleted in Bambuddy', async ({ page }) => {
    await page.goto('/prints/38')
    await expect(page.getByRole('status')).toContainText('deleted in Bambuddy')
    await expect(page.getByRole('region', { name: 'Files' }).getByRole('listitem')).toHaveCount(2)
    await expect(page.getByRole('button', { name: 'Print again' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Open in Bambuddy' })).toHaveCount(0)
  })

  test('works inside Bambuddy’s sandboxed frame', async ({ page, baseURL }) => {
    const frame = await framed(page, baseURL, '/prints/36')

    // Pull a timelapse the printer still has, only once asked.
    const timelapse = frame.getByRole('region', { name: 'Timelapse' })
    await timelapse.getByRole('button', { name: 'Look on the printer' }).click()
    await timelapse.getByRole('button', { name: 'Pull timelapse from printer' }).click()
    await expect(timelapse.locator('video')).toHaveAttribute('src', '/api/v1/prints/36/timelapse')

    // Chromium drops a download started in the sandboxed frame; it is saved from a
    // blank popup that escapes the sandbox instead.
    const popup = page.context().waitForEvent('page')
    await frame.getByRole('region', { name: 'Files' }).getByRole('button', { name: 'Download name-keychain-reagan.gcode.3mf' }).click()
    const saved = await (await popup).waitForEvent('download')
    expect(saved.suggestedFilename()).toBe('name-keychain-reagan.gcode.3mf')

    // Open in Bambuddy escapes the frame into a new tab.
    await page.context().route('https://bambuddy.example/**', (route) => route.fulfill({ body: 'Bambuddy' }))
    const tab = page.context().waitForEvent('page')
    await frame.getByRole('button', { name: 'Open in Bambuddy' }).click()
    const bambuddy = await tab
    await bambuddy.waitForLoadState()
    expect(bambuddy.url()).toBe('https://bambuddy.example/archives')

    await frame.getByRole('button', { name: 'Print again' }).click()
    const dialog = frame.getByRole('dialog', { name: 'Print again' })
    await dialog.getByRole('button', { name: 'Queue' }).click()
    await expect(dialog).toContainText('Queued as #')
  })
})

/** Bambuddy's External Link frame: another origin and its sandbox flags. */
async function framed(page: Page, baseURL: string | undefined, path: string): Promise<FrameLocator> {
  const frame = await bambuddyFrame(page, baseURL, path)
  await expect(frame.getByRole('heading', { level: 1 })).toBeVisible({ timeout: 15_000 })
  return frame
}
