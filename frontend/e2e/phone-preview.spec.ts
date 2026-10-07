import { expect, test, type Page } from '@playwright/test'

// #1741 — at a phone's size the preview was a 105 px strip under the parameters and an
// action bar of every button and the project picker. msw-backed: the templates are fixtures.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

const MODELS = [
  { name: 'a generated form', path: '/m/name-keychain' },
  { name: "a template's own panel", path: '/m/builtin%3Amaze-puzzle' },
]

async function open(page: Page, path: string) {
  await page.goto(path)
  await expect(page.getByTestId('bbox-readout')).toBeVisible()
}

for (const size of [
  { width: 390, height: 844 },
  { width: 360, height: 640 },
]) {
  test.describe(`customize at ${size.width}x${size.height} (#1741)`, () => {
    test.use({ viewport: size })

    for (const model of MODELS) {
      test(`${model.name}: the preview has room and Generate is on screen`, async ({ page }) => {
        await open(page, model.path)
        const canvas = await page.getByTestId('preview-canvas').boundingBox()
        expect(canvas?.height).toBeGreaterThanOrEqual(240)
        await expect(page.getByTestId('generate')).toBeInViewport({ ratio: 1 })
        // One row: the other actions wait behind More.
        await expect(page.getByTestId('more-actions')).toBeVisible()
        await expect(page.getByRole('button', { name: 'Download 3MF' })).toBeHidden()
        await expect(page.getByTestId('customize-project-select')).toBeHidden()
      })
    }

    test('More holds the project, Download, Send and Print', async ({ page }) => {
      await open(page, '/m/name-keychain')
      await page.getByTestId('generate').click()
      await expect(page.getByText(/^Saved /)).toBeVisible()
      await page.getByTestId('more-actions').click()
      await expect(page.getByTestId('customize-project-select')).toBeInViewport()
      for (const name of ['Download 3MF', 'Send to Bambuddy']) {
        await expect(page.getByRole('button', { name })).toBeInViewport()
      }
      await expect(page.getByTestId('print')).toBeInViewport()
      await page.keyboard.press('Escape')
      await expect(page.getByTestId('print')).toBeHidden()
      await expect(page.getByTestId('more-actions')).toBeFocused()
    })
  })
}

test.describe('customize on a desktop (#1741)', () => {
  test.use({ viewport: { width: 1440, height: 900 } })

  test('the action bar shows every action inline', async ({ page }) => {
    await open(page, '/m/name-keychain')
    await expect(page.getByTestId('more-actions')).toBeHidden()
    await expect(page.getByTestId('customize-project-select')).toBeVisible()
    for (const name of ['Download 3MF', 'Send to Bambuddy']) {
      await expect(page.getByRole('button', { name })).toBeVisible()
    }
    await expect(page.getByTestId('print')).toBeVisible()
    await expect(page.getByTestId('generate')).toBeVisible()
  })
})
