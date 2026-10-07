import { expect, test, type Page } from '@playwright/test'

// #1743, #1744 — the preview's readouts and its notes and warnings, laid over the scene,
// must stay inside it, leave most of it to the model, and let a drag orbit the camera.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

interface Box {
  x: number
  y: number
  width: number
  height: number
}

interface Overlays {
  preview: Box
  chips: Box[]
  panels: Box[]
}

/** The preview and every visible box laid over it: its chips (and the page's buttons), and the message panels. */
async function overlays(page: Page): Promise<Overlays> {
  return (await page.evaluate(`(() => {
    const rect = (el) => {
      const r = el.getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: r.height }
    }
    const visible = (el) => {
      const r = el.getBoundingClientRect()
      return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'
    }
    const layer = document.querySelector('[data-testid="preview-overlay"]')
    const chips = new Set([...layer.querySelectorAll('[data-overlay="chip"], button')].filter(visible))
    const panels = [...layer.querySelectorAll('[data-overlay="panel"]')].filter(visible)
    return {
      preview: rect(document.querySelector('[data-testid="preview"]')),
      chips: [...chips].map(rect),
      panels: panels.map(rect),
    }
  })()`)) as Overlays
}

const area = (boxes: Box[]) => boxes.reduce((sum, box) => sum + box.width * box.height, 0)

function inside(box: Box, outer: Box): boolean {
  return (
    box.x >= outer.x - 0.5 &&
    box.y >= outer.y - 0.5 &&
    box.x + box.width <= outer.x + outer.width + 0.5 &&
    box.y + box.height <= outer.y + outer.height + 0.5
  )
}

/** A render with the template's notes, ScadBuddy's warnings and OpenSCAD's, all at once. */
async function crowdedRender(page: Page, shortPreview: boolean) {
  await page.goto('/m/name-keychain')
  if (shortPreview) {
    await page.addStyleTag({ content: '[data-testid="preview"] { height: 120px !important; }' })
  }
  // The viewer is the page's largest lazy chunk (three.js), as downloads.spec.ts allows for.
  await expect(page.getByTestId('bbox-readout')).toBeVisible({ timeout: 15_000 })
  await page.getByRole('textbox', { name: 'Name on the tag' }).fill('crowded')
  // Either the panels or the chip that folds them.
  await expect(
    page
      .getByRole('region', { name: 'Notes from the template' })
      .or(page.getByRole('button', { name: '2 warnings · 2 notes' })),
  ).toBeVisible()
  await expect(page.getByText('Rendering')).toHaveCount(0)
}

/** A screenshot of the scene alone, after it has stopped moving. */
async function settledScene(page: Page): Promise<Buffer> {
  const canvas = page.getByTestId('preview').locator('canvas')
  let last = await canvas.screenshot()
  for (let tries = 0; tries < 20; tries += 1) {
    await page.waitForTimeout(150)
    const next = await canvas.screenshot()
    if (next.equals(last)) return next
    last = next
  }
  return last
}

const sizes = [
  { name: '1440×900', viewport: { width: 1440, height: 900 }, short: false },
  { name: '1280×720', viewport: { width: 1280, height: 720 }, short: false },
  { name: '390×844', viewport: { width: 390, height: 844 }, short: false },
  { name: 'a 120 px preview at 390', viewport: { width: 390, height: 844 }, short: true },
]

for (const size of sizes) {
  test.describe(`preview overlays at ${size.name}`, () => {
    test.use({ viewport: size.viewport })

    test('stay inside the preview and leave most of it to the model', async ({ page }) => {
      await crowdedRender(page, size.short)
      const { preview, chips, panels } = await overlays(page)
      const canvas = preview.width * preview.height
      expect(canvas).toBeGreaterThan(0)
      test.info().annotations.push({
        type: 'coverage',
        description: `preview ${Math.round(preview.width)}×${Math.round(preview.height)}: chips ${(
          (100 * area(chips)) / canvas
        ).toFixed(1)}%, panels ${((100 * area(panels)) / canvas).toFixed(1)}%`,
      })

      for (const box of [...chips, ...panels]) expect(inside(box, preview), JSON.stringify({ box, preview })).toBe(true)
      expect(area(chips) / canvas).toBeLessThanOrEqual(0.25)
      expect(area(panels) / canvas).toBeLessThanOrEqual(1 / 3)
      // The page's full-screen button stays reachable.
      await expect(page.getByRole('button', { name: 'Full screen' })).toBeInViewport()
    })

    test('a drag from the centre of the preview orbits the camera', async ({ page }) => {
      await crowdedRender(page, size.short)
      const { preview } = await overlays(page)
      const x = preview.x + preview.width / 2
      const y = preview.y + preview.height / 2
      expect(await page.evaluate(`document.elementFromPoint(${x}, ${y})?.tagName`)).toBe('CANVAS')

      const before = await settledScene(page)
      await page.mouse.move(x, y)
      await page.mouse.down()
      await page.mouse.move(x + 60, y + 10, { steps: 6 })
      await page.mouse.up()
      const after = await settledScene(page)
      expect(after.equals(before)).toBe(false)
    })
  })
}

test.describe('the folded notes and warnings', () => {
  test.use({ viewport: { width: 390, height: 844 } })

  test('open from their chip, keep their names and close with Escape', async ({ page }) => {
    await crowdedRender(page, true)
    const chip = page.getByRole('button', { name: '2 warnings · 2 notes' })
    await chip.click()
    await expect(page.getByRole('region', { name: 'Notes from the template' })).toContainText(
      'text_size reduced from 14 to 9.5 mm',
    )
    await expect(page.getByRole('region', { name: 'Render warnings' })).toContainText(
      'OpenSCAD could not open pic.svg',
    )
    await expect(page.getByRole('region', { name: 'OpenSCAD warnings' })).toContainText(
      'module cube() does not support child modules',
    )
    await page.keyboard.press('Escape')
    await expect(page.getByRole('region', { name: 'Notes from the template' })).toHaveCount(0)
    await expect(chip).toBeFocused()
  })
})
