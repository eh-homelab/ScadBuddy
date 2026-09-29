import { expect, test } from '@playwright/test'

// Real multipart bodies through the msw worker: jsdom cannot build one, so this is
// where the upload of a model's folder and of a cover image are exercised (#179).
const PNG = Buffer.from('89504e470d0a1a0a0000000d49484452', 'hex')

test.describe('model details', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('shows a model with no thumbnail and no output by its default-render preview', async ({
    page,
  }) => {
    await page.goto('/')
    const card = page.getByRole('listitem').filter({ hasText: 'Keychain Template' })
    const image = card.getByRole('img', { name: 'Keychain Template' })

    await expect(image).toBeVisible()
    // Keyed on which render it is, so a re-render after a source edit refetches.
    await expect(image).toHaveAttribute('src', /\/thumbnail\?v=[^"]*5eed0f00d5eed0f0/)
  })

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

  test('shows the server refusing a model.json that is not JSON', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Add model' }).click()
    const dialog = page.getByRole('dialog', { name: 'Add a model' })

    await dialog.getByLabel('OpenSCAD source file').setInputFiles([
      { name: 'model.scad', mimeType: 'text/plain', buffer: Buffer.from('cube(10);\n') },
      { name: 'model.json', mimeType: 'application/json', buffer: Buffer.from('{not json') },
    ])
    await dialog.getByRole('button', { name: 'Add model' }).click()

    await expect(dialog.getByRole('alert')).toHaveText('the model.json is not valid JSON')
  })

  test('edits a model’s name, thumbnail and README after it was created', async ({ page }) => {
    await page.goto('/m/gridfinity-bin')
    await page.getByRole('button', { name: 'Edit details' }).click()
    const dialog = page.getByRole('dialog', { name: 'Edit details' })

    await dialog.getByLabel('Name').fill('Gridfinity Bin 2x3')
    // The thumbnail comes from the template's media: uploaded in the picker, it is
    // added to the media at once and becomes the cover.
    await dialog.getByRole('button', { name: 'Choose…' }).click()
    const picker = page.getByRole('dialog', { name: 'Choose the thumbnail' })
    await picker
      .getByLabel('Upload a file')
      .setInputFiles({ name: 'cover.png', mimeType: 'image/png', buffer: PNG })
    await expect(picker).toBeHidden()
    await expect(dialog.getByTestId('thumbnail-state')).toContainText('cover.png becomes the cover')
    await dialog.getByLabel('README', { exact: true }).fill('# Bin\n\nPrints without supports.\n')
    await dialog.getByRole('button', { name: 'Save' }).click()
    await expect(dialog).toBeHidden()

    await page.getByRole('link', { name: 'Versions' }).click()
    // One revision per change, newest first.
    const revisions = page.getByRole('button', { name: /gridfinity-bin/ })
    await expect(revisions.nth(0)).toContainText('Set gridfinity-bin README')
    await expect(revisions.nth(1)).toContainText('Update gridfinity-bin metadata')
    // The only image, so already the cover: Save had no reorder to make.
    await expect(revisions.nth(2)).toContainText('Add media to gridfinity-bin')

    // In-app, not `goto`: a reload would restart the mock backend's state.
    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Models' }).click()
    const card = page.getByRole('listitem').filter({ hasText: 'Gridfinity Bin 2x3' })
    await expect(card.getByRole('img', { name: 'Gridfinity Bin 2x3' })).toBeVisible()
    await expect(card.getByText(/not generated yet/)).toHaveCount(0)
  })
})
