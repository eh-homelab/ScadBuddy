import { expect, test } from '@playwright/test'

// #279 — real multipart media uploads through the msw worker: jsdom cannot build one,
// so the vitest suites stub `uploadMedia` and this is where the XHR path runs.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4DwABAQEABRjYTgAAAABJRU5ErkJggg==',
  'base64',
)
const MP4 = Buffer.from('AAAAGGZ0eXBpc29tAAACAGlzb21pc28y', 'base64')

test.describe('media manager', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('uploads images and a video, then makes the video the cover', async ({ page }) => {
    await page.goto('/m/gridfinity-bin')
    await page.getByRole('button', { name: 'Media', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Media' })
    await expect(dialog.getByText('No images or videos yet.')).toBeVisible()

    await dialog.getByLabel('Add images or videos').setInputFiles([
      { name: 'front.png', mimeType: 'image/png', buffer: PNG },
      { name: 'print.mp4', mimeType: 'video/mp4', buffer: MP4 },
    ])

    const items = dialog.getByRole('list', { name: 'Media items' }).getByRole('listitem')
    await expect(items).toHaveCount(2)
    await expect(items.nth(0)).toContainText('Cover')
    await expect(items.nth(0)).toContainText('Image')
    await expect(items.nth(1)).toContainText('Video')

    await items.nth(1).getByRole('button', { name: 'Make cover' }).click()
    await expect(items.nth(0)).toContainText('Video')
    await expect(items.nth(0)).toContainText('Cover')
  })

  test('adds a model with several media, the first as its cover', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Add model' }).click()
    const upload = page.getByRole('dialog', { name: 'Add a model' })

    await upload
      .getByLabel('OpenSCAD source file')
      .setInputFiles({ name: 'Media Widget.scad', mimeType: 'text/plain', buffer: Buffer.from('cube(10);\n') })
    await upload.getByLabel('Images and videos').setInputFiles([
      { name: 'print.mp4', mimeType: 'video/mp4', buffer: MP4 },
      { name: 'front.png', mimeType: 'image/png', buffer: PNG },
    ])
    await upload.getByRole('button', { name: 'Add model' }).click()

    await expect(page).toHaveURL(/\/m\/media-widget$/)
    await page.getByRole('button', { name: 'Media', exact: true }).click()
    const items = page
      .getByRole('dialog', { name: 'Media' })
      .getByRole('list', { name: 'Media items' })
      .getByRole('listitem')
    await expect(items).toHaveCount(2)
    await expect(items.nth(0)).toContainText('Video')
    await expect(items.nth(0)).toContainText('Cover')
  })
})

// #1037 — at a phone's width each row's buttons sat beside the caption and squeezed it to
// ~23 px, with the Cover badge drawn over Move up.
test.describe('media manager at 390 px (#1037)', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )
  test.use({ viewport: { width: 390, height: 844 } })

  test('a caption has room to read, and the Cover badge clears the buttons', async ({ page }) => {
    await page.goto('/m/gridfinity-bin')
    await page.getByRole('button', { name: 'Media', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Media' })
    await dialog.getByLabel('Add images or videos').setInputFiles([
      { name: 'front.png', mimeType: 'image/png', buffer: PNG },
      { name: 'side.png', mimeType: 'image/png', buffer: PNG },
    ])
    const items = dialog.getByRole('list', { name: 'Media items' }).getByRole('listitem')
    await expect(items).toHaveCount(2)

    for (const caption of await dialog.getByLabel('Caption').all()) {
      expect((await caption.boundingBox())!.width).toBeGreaterThanOrEqual(150)
    }
    const badge = (await items.nth(0).getByText('Cover', { exact: true }).boundingBox())!
    for (const name of ['Move up', 'Move down', 'Delete']) {
      const button = (await items.nth(0).getByRole('button', { name, exact: true }).boundingBox())!
      const overlaps =
        badge.x < button.x + button.width &&
        button.x < badge.x + badge.width &&
        badge.y < button.y + button.height &&
        button.y < badge.y + badge.height
      expect(overlaps, `Cover overlaps ${name}`).toBe(false)
    }
  })
})
