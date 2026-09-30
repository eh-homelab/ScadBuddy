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

  test('frames the image in the dialog without moving the viewer (#722)', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toContainText('64.1')
    const canvas = page.getByTestId('preview-canvas').locator('canvas')
    const view = (await canvas.boundingBox())!
    const before = await canvas.screenshot()

    await page.getByTestId('generate-menu').click()
    await page.getByTestId('generate-image').click()
    const dialog = page.getByRole('dialog', { name: 'Rendered image' })
    await expect(dialog.getByTestId('image-preview')).toBeVisible()
    const first = await dialog.getByTestId('image-preview').getAttribute('src')

    await dialog.getByLabel('Square').check()
    const surface = dialog.getByTestId('image-framing')
    const box = (await surface.boundingBox())!
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
    await page.mouse.down()
    await page.mouse.move(box.x + box.width / 2 + 80, box.y + box.height / 2 + 10, { steps: 5 })
    await page.mouse.up()
    await page.mouse.wheel(0, -300)
    await expect(dialog.getByTestId('image-preview')).not.toHaveAttribute('src', first!)

    const saved = page.waitForEvent('download')
    await dialog.getByTestId('image-save').click()
    const size = pngSize(await readFile((await (await saved).path())!))
    const edge = Math.floor(Math.max(view.width, view.height) * 2)
    expect(size).toEqual({ width: edge, height: edge })

    await dialog.getByRole('button', { name: 'Cancel' }).click()
    // The viewer's own camera is where it was.
    expect((await canvas.screenshot()).equals(before)).toBe(true)
  })
})

test.describe('rendered image in the media', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed')

  test('adds the image as the cover, then offers it to a file parameter', async ({ page }) => {
    await page.goto('/m/gridfinity-bin')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    await page.getByTestId('generate-menu').click()
    await page.getByTestId('generate-image').click()
    const dialog = page.getByRole('dialog', { name: 'Rendered image' })
    await expect(dialog.getByTestId('image-preview')).toBeVisible()
    await dialog.getByTestId('image-add-cover').click()
    // A 2× render and a multi-megabyte upload, both in software WebGL here.
    await expect(dialog.getByText('Added to the media as the cover')).toBeVisible({
      timeout: 30_000,
    })
    await dialog.getByRole('button', { name: 'Done' }).click()

    // The page's gallery has it at once.
    await expect(page.getByRole('list', { name: 'Gallery' }).getByRole('button')).toHaveCount(1)

    // Edit details shows it as the cover in its picker.
    await page.getByRole('button', { name: 'Edit details' }).click()
    const details = page.getByRole('dialog', { name: 'Edit details' })
    await expect(details.getByTestId('thumbnail-state')).toContainText('Set on this model')
    await details.getByRole('button', { name: 'Change…' }).click()
    const coverPicker = page.getByRole('dialog', { name: 'Choose the thumbnail' })
    await expect(coverPicker.getByRole('button', { name: 'Choose Image 1' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    await coverPicker.getByRole('button', { name: 'Cancel' }).click()
    await details.getByRole('button', { name: 'Cancel' }).click()

    // And a file parameter can take it, as a PNG of its own.
    await page.getByRole('tab', { name: 'Features' }).click()
    const rendered = page.waitForRequest(
      (request) =>
        request.method() === 'POST' &&
        request.url().endsWith('/render') &&
        /^[0-9a-f]{64}$/.test(String(request.postDataJSON()?.params?.label_art ?? '')),
    )
    await page.getByRole('button', { name: 'Choose…' }).click()
    const picker = page.getByRole('dialog', { name: 'Choose Label artwork' })
    await picker
      .getByRole('region', { name: "Template's images" })
      .getByRole('button', { name: 'Choose Image 1' })
      .click()
    await expect(picker).toBeHidden()
    await expect(page.getByText('Image 1.png')).toBeVisible()
    await rendered
  })
})
