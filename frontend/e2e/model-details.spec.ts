import { expect, test } from '@playwright/test'

// Real multipart bodies through the msw worker: jsdom cannot build one, so this is
// where the upload of a model's folder and the thumbnail PUT are exercised (#179).
const PNG = Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex')

test.describe('model details', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('adds a model from its files, with its metadata, thumbnail and README', async ({
    page,
  }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Add model' }).click()
    const dialog = page.getByRole('dialog', { name: 'Add a model' })

    await dialog.getByLabel('OpenSCAD source file').setInputFiles([
      { name: 'model.scad', mimeType: 'text/plain', buffer: Buffer.from('cube(10);\n') },
      {
        name: 'model.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify({ name: 'Widget Deluxe', tags: ['widget'] })),
      },
      { name: 'thumbnail.png', mimeType: 'image/png', buffer: PNG },
      { name: 'README.md', mimeType: 'text/markdown', buffer: Buffer.from('# Widget\n') },
    ])
    await expect(dialog.getByTestId('upload-meta')).toContainText('Widget Deluxe')
    await dialog.getByRole('button', { name: 'Add model' }).click()

    // Named after the model.json, not after `model.scad`.
    await expect(page).toHaveURL(/\/m\/widget-deluxe$/)
    await page.getByRole('main').getByRole('link', { name: 'Models' }).click()
    const card = page.getByRole('listitem').filter({ hasText: 'Widget Deluxe' })
    await expect(card.getByRole('img', { name: 'Widget Deluxe' })).toBeVisible()
    await expect(card.getByText('widget', { exact: true })).toBeVisible()
  })

  test('edits a model’s name, thumbnail and README after it was created', async ({ page }) => {
    await page.goto('/m/gridfinity-bin')
    await page.getByRole('button', { name: 'Edit details' }).click()
    const dialog = page.getByRole('dialog', { name: 'Edit details' })

    await dialog.getByLabel('Name').fill('Gridfinity Bin 2x3')
    await dialog
      .getByLabel('Thumbnail (PNG)')
      .setInputFiles({ name: 'cover.png', mimeType: 'image/png', buffer: PNG })
    await dialog.getByLabel('README', { exact: true }).fill('# Bin\n\nPrints without supports.\n')
    await dialog.getByRole('button', { name: 'Save' }).click()
    await expect(dialog).toBeHidden()

    await page.getByRole('link', { name: 'Versions' }).click()
    // One revision per change, newest first.
    const revisions = page.getByRole('button', { name: /gridfinity-bin/ })
    await expect(revisions.nth(0)).toContainText('Set gridfinity-bin README')
    await expect(revisions.nth(1)).toContainText('Set gridfinity-bin thumbnail')
    await expect(revisions.nth(2)).toContainText('Update gridfinity-bin metadata')

    // In-app, not `goto`: a reload would restart the mock backend's state.
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Models' }).click()
    const card = page.getByRole('listitem').filter({ hasText: 'Gridfinity Bin 2x3' })
    await expect(card.getByRole('img', { name: 'Gridfinity Bin 2x3' })).toBeVisible()
    await expect(card.getByText(/not generated yet/)).toHaveCount(0)
  })
})
