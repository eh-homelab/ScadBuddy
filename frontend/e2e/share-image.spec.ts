import { readFile } from 'node:fs/promises'
import { expect, test } from '@playwright/test'

/** A PNG's width and height, from its IHDR chunk. */
function pngSize(bytes: Buffer): { width: number; height: number } {
  return { width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) }
}

test.describe('rendered image', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed')

  test('saves a high-resolution PNG of the view from Generate’s menu', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toContainText('64.1')

    await page.getByTestId('generate-menu').click()
    await page.getByTestId('generate-image').click()
    const dialog = page.getByRole('dialog', { name: 'Rendered image' })
    await expect(dialog.getByTestId('image-preview')).toBeVisible()

    const view = await page.getByTestId('preview-canvas').locator('canvas').boundingBox()
    await dialog.getByLabel(/3×/).check()
    await dialog.getByTestId('image-plate').uncheck()

    const saved = page.waitForEvent('download')
    await dialog.getByTestId('image-save').click()
    const download = await saved
    expect(download.suggestedFilename()).toBe('name-keychain-render.png')
    const bytes = await readFile((await download.path())!)
    const size = pngSize(bytes)
    expect(size.width).toBe(Math.floor(view!.width * 3))
    expect(size.height).toBe(Math.floor(view!.height * 3))
    await test.info().attach('render.png', { body: bytes, contentType: 'image/png' })
  })
})
