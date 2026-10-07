import { expect, test, type Locator, type Page } from '@playwright/test'

// #1737–#1740 — the dollhouse designer (a page-slot template UI) at a phone's, a short
// window's and a laptop's size. msw-backed: src/mocks/ui/dollhouse-kit/ is a copy of the
// template's ui/.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

async function open(page: Page): Promise<Locator> {
  await page.goto('/m/builtin%3Adollhouse-kit')
  const ui = page.getByTestId('template-ui')
  // The designer has drawn its piece list. The page gives a template's module as long as
  // `MOUNT_TIMEOUT_MS` (15 s, TemplateUi.tsx) to load and mount; with every worker rendering at once, the expect
  // default of 5 s was not enough.
  await expect(ui.locator('button[data-entry]').first()).toBeAttached({ timeout: 15_000 })
  return ui
}

async function height(locator: Locator): Promise<number> {
  const box = await locator.boundingBox()
  return box?.height ?? 0
}

for (const viewport of [
  { width: 390, height: 844 },
  { width: 360, height: 640 },
]) {
  test.describe(`at ${viewport.width}×${viewport.height} (#1737)`, () => {
    test.use({ viewport })

    test('the preview and the piece list are on the page, and the preview is usable', async ({ page }) => {
      const ui = await open(page)
      const preview = ui.locator('sb-preview')
      await preview.scrollIntoViewIfNeeded()
      await expect(preview).toBeInViewport()
      expect(await height(preview)).toBeGreaterThanOrEqual(200)

      const show = ui.locator('button[data-entry]').first()
      await show.scrollIntoViewIfNeeded()
      await expect(show).toBeInViewport()
    })
  })
}

test.describe('at 844×390, a short landscape window (#1738)', () => {
  test.use({ viewport: { width: 844, height: 390 } })

  test('the preview is at most one viewport tall', async ({ page }) => {
    const ui = await open(page)
    const preview = ui.locator('sb-preview')
    await expect(preview).toBeAttached()
    // Long enough for a canvas that sizes itself to its box to have fed back.
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    expect(await height(preview)).toBeLessThanOrEqual(390)
    expect(await height(preview)).toBeGreaterThan(0)
  })
})

test.describe('at 1280×800, a laptop (#1740)', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  // The issue asks for at least 40%; the mock's page chrome is shorter than the live one's,
  // and gave 42% before the fix, so the check is the larger share.
  test('the preview has the larger share of the designer', async ({ page }) => {
    const ui = await open(page)
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    const designer = await height(ui)
    expect(designer).toBeGreaterThan(0)
    expect(await height(ui.locator('sb-preview'))).toBeGreaterThanOrEqual(designer * 0.5)
  })

  test('one Generate control, the page’s own', async ({ page }) => {
    await open(page)
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    const workspace = page.getByTestId('workspace')
    await expect(workspace.getByRole('button', { name: /^(generat|render)/i })).toHaveCount(1)
    await expect(page.getByTestId('template-ui').locator('sb-generate')).toHaveCount(0)
  })
})

interface Control {
  height: number
  /** A border or a background: something that reads as a control, not as text. */
  visible: boolean
  cursor: string
}

/** The designer's controls matching `selector`, measured in its shadow root (#1739). A
 *  string, as phone-width.spec.ts's: the e2e tsconfig has no DOM lib. */
async function controls(page: Page, selector: string): Promise<Control[]> {
  return (await page.evaluate(`(() => {
    const root = document.querySelector('[data-testid="template-ui"]').shadowRoot
    return Array.from(root.querySelectorAll(${JSON.stringify(selector)})).map((el) => {
      const style = getComputedStyle(el)
      const border = parseFloat(style.borderTopWidth) > 0 && style.borderTopStyle !== 'none'
      const fill = style.backgroundColor !== 'rgba(0, 0, 0, 0)' && style.backgroundColor !== 'transparent'
      return { height: el.getBoundingClientRect().height, visible: border || fill, cursor: style.cursor }
    })
  })()`)) as Control[]
}

test.describe('the Show buttons look and act like buttons (#1739)', () => {
  test.use({ viewport: { width: 1280, height: 800 } })

  test('at least 32 px tall, with a border or background', async ({ page }) => {
    await open(page)
    const buttons = await controls(page, 'button[data-entry]')
    expect(buttons.length).toBeGreaterThan(0)
    for (const button of buttons) {
      expect(button.height).toBeGreaterThanOrEqual(32)
      expect(button.visible).toBe(true)
      expect(button.cursor).toBe('pointer')
    }
  })

  test('the house-size inputs have a border or background', async ({ page }) => {
    await open(page)
    const inputs = await controls(page, 'input[type="number"]')
    expect(inputs.length).toBeGreaterThanOrEqual(4)
    for (const input of inputs) {
      expect(input.height).toBeGreaterThanOrEqual(32)
      expect(input.visible).toBe(true)
    }
  })
})

test.describe('under a coarse pointer (#1739)', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true })

  test('Show buttons are at least 44 px tall', async ({ page }) => {
    await open(page)
    expect(await page.evaluate("matchMedia('(pointer: coarse)').matches")).toBe(true)
    const buttons = await controls(page, 'button[data-entry]')
    expect(buttons.length).toBeGreaterThan(0)
    for (const button of buttons) expect(button.height).toBeGreaterThanOrEqual(44)
  })
})
