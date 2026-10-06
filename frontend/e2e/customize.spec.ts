import { expect, test, type Frame, type Page } from '@playwright/test'

/**
 * Where `count` presses of Tab land, as seen from `where` (the page, or the frame that
 * holds the app): `view` inside the full-screen workspace, `page` for the document
 * itself (past the last control, before the browser wraps round), or else the label of
 * whatever outside the view took the focus.
 */
async function tabStops(page: Page, where: Page | Frame, count: number): Promise<string[]> {
  const stops: string[] = []
  for (let press = 0; press < count; press += 1) {
    await page.keyboard.press('Tab')
    stops.push(
      String(
        await where.evaluate(`(() => {
          const focused = document.activeElement
          if (!focused || focused === document.body) return 'page'
          if (focused.closest('[data-testid="workspace"]')) return 'view'
          return focused.getAttribute('aria-label') || focused.textContent.trim()
        })()`),
      ),
    )
  }
  return stops
}

test.describe('customizer', () => {
  // These drive the msw worker. Against a real backend the numbers are the real
  // renderer's, which e2e/real-backend.spec.ts covers instead.
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('opens a model, changes a parameter and generates an output', async ({ page }) => {
    await page.goto('/')

    await expect(page.getByRole('heading', { name: 'Models' })).toBeVisible()
    await page.getByRole('link', { name: /Name Keychain/ }).click()

    // The first render happens without being asked (spec §5.3).
    const bbox = page.getByTestId('bbox-readout')
    await expect(bbox).toContainText('64.1 × 37.2 × 6.8 mm')
    await expect(page.getByTestId('preview-canvas')).toBeVisible()

    const name = page.getByRole('textbox', { name: 'Name on the tag' })
    await name.fill('Nova')
    await expect(bbox).toContainText('46.7 × 37.2 × 6.8 mm')

    const generate = page.getByTestId('generate')
    await expect(generate).toBeEnabled()
    await generate.click()

    await expect(page.getByText(/^Saved /)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Download 3MF' })).toBeEnabled()
    await expect(page.getByRole('button', { name: 'Send to Bambuddy' })).toBeEnabled()
  })

  test('shows every parameter group tab however narrow the panel, none scrolled out of sight (#942)', async ({
    page,
  }) => {
    await page.goto('/m/name-keychain')
    const tablist = page.getByRole('tablist', { name: 'Parameter groups' })
    await expect(tablist.getByRole('tab')).toHaveCount(3)
    // Narrower than the three tabs side by side: a template with many groups
    // (Dollhouse Kit has 15) overflows the panel at any width.
    await tablist.evaluate((element) => element.setAttribute('style', 'width: 90px'))

    const box = await tablist.boundingBox()
    if (!box) throw new Error('the tablist is not laid out')
    for (const tab of await tablist.getByRole('tab').all()) {
      const tabBox = await tab.boundingBox()
      if (!tabBox) throw new Error('a tab is not laid out')
      // Inside the tablist, not past its edge behind a hidden scrollbar.
      expect(tabBox.x).toBeGreaterThanOrEqual(box.x - 0.5)
      expect(tabBox.x + tabBox.width).toBeLessThanOrEqual(box.x + box.width + 0.5)
    }
    expect(await tablist.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)
  })

  test('keeps the previous preview while the next render runs', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toContainText('64.1')

    await page.getByRole('textbox', { name: 'Name on the tag' }).fill('Workshop')
    await expect(page.getByText('Rendering')).toBeVisible()
    // The old dimensions stay on screen rather than blanking out.
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await expect(page.getByTestId('bbox-readout')).toContainText('81.4', { timeout: 10_000 })
  })

  test('follows a render over the realtime socket instead of polling it (#267)', async ({
    page,
  }) => {
    const reads: string[] = []
    page.on('request', (request) => {
      const path = new URL(request.url()).pathname
      if (request.method() === 'GET' && /^\/api\/v1\/jobs\/[0-9a-f]{32}$/.test(path)) {
        reads.push(path)
      }
    })
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toContainText('64.1')
    // One read when the subscription is confirmed and one per state the job announced
    // (running, done): at 400 ms polling a render would keep reading until it settled.
    expect(reads.length).toBeLessThanOrEqual(3)
    const settled = reads.length
    await page.waitForTimeout(1_500)
    expect(reads).toHaveLength(settled)
    await expect(page.getByText('Live updates unavailable')).toBeHidden()
  })

  test('shows the view full screen with the parameters in a flyout, and puts it back', async ({
    page,
  }) => {
    await page.goto('/m/name-keychain')
    const bbox = page.getByTestId('bbox-readout')
    await expect(bbox).toContainText('64.1')
    const canvas = page.getByTestId('preview-canvas')
    const docked = await canvas.boundingBox()
    const parameters = page.getByRole('region', { name: 'Parameters' })
    const name = page.getByRole('textbox', { name: 'Name on the tag' })

    await page.getByRole('button', { name: 'Full screen', exact: true }).click()
    const exit = page.getByRole('button', { name: 'Exit full screen' })
    await expect(exit).toBeVisible()
    // The browser's own full screen: the view alone, its readouts still over the scene.
    expect(await page.evaluate('document.fullscreenElement !== null')).toBe(true)
    const viewport = page.viewportSize()
    const whole = { x: 0, y: 0, width: viewport?.width, height: viewport?.height }
    await expect.poll(() => canvas.boundingBox()).toEqual(whole)
    await expect(bbox).toContainText('64.1')
    await expect(parameters).toBeHidden()
    await expect(page.getByTestId('generate')).toBeHidden()
    // Tab stays in the view: nothing full screen hides takes the focus. The browser's own
    // full screen sees to that; the stand-in's inert page is checked in the frame below.
    const stops = await tabStops(page, page, 8)
    expect(stops).toContain('view')
    expect(stops.filter((stop) => stop !== 'view' && stop !== 'page')).toEqual([])

    // The parameters fly out over the scene, and a change renders while in full screen.
    await page.getByRole('button', { name: 'Parameters', exact: true }).click()
    await expect
      .poll(() => parameters.boundingBox())
      .toEqual({ x: 0, y: 0, width: 360, height: viewport?.height })
    await name.fill('Nova')
    await expect(bbox).toContainText('46.7 × 37.2 × 6.8 mm')
    // The readouts move clear of it; the scene stays where it was.
    expect((await bbox.boundingBox())?.x).toBeGreaterThanOrEqual(360)
    expect(await canvas.boundingBox()).toEqual(whole)

    // A dialog from the flyout, in the browser's own full screen. The page's part of
    // Escape: the dialog takes it, and nothing in the page leaves full screen for it.
    // A real browser leaves anyway, on its own and past any page's reach (spec §5.3);
    // automated Chromium never hands it the key, so that half is checked by hand.
    await page.getByRole('button', { name: 'Browse' }).click()
    const picker = page.getByRole('dialog', { name: 'Choose a font' })
    await expect(picker).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(picker).toBeHidden()
    expect(await page.evaluate('document.fullscreenElement !== null')).toBe(true)
    await page.getByRole('button', { name: 'Close parameters' }).click()
    await expect(parameters).toBeHidden()

    await exit.click()
    await expect(page.getByRole('button', { name: 'Full screen', exact: true })).toBeVisible()
    expect(await page.evaluate('document.fullscreenElement')).toBeNull()
    // Back in its place and no wider: the canvas is sized in pixels, and its full-screen
    // width must not hold the column open.
    await expect.poll(() => canvas.boundingBox()).toEqual(docked)
    await expect(name).toHaveValue('Nova')
  })

  test('fills the frame where the page may not go full screen, as inside Bambuddy', async ({
    page,
    context,
    baseURL,
    browserName,
  }) => {
    // Bambuddy's External Link frame: another origin, its sandbox flags and no
    // allow="fullscreen", so the Fullscreen API is refused inside it. The page around it
    // only has to be on a second origin; a static file there will do.
    const host = new URL('/mockServiceWorker.js', baseURL)
    host.hostname = host.hostname === 'localhost' ? '127.0.0.1' : 'localhost'
    // Bambuddy's page, not ours: served without ScadBuddy's page CSP, which would refuse
    // to frame another origin.
    await page.route(host.href, (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html>' }))
    // A routed page is not on the loopback address space, so Chrome's Local Network
    // Access checks refuse its loopback frame. Bambuddy on the LAN is local; grant it.
    // Chromium's permission: another browser's context throws on the unknown name.
    if (browserName === 'chromium') await context.grantPermissions(['local-network-access'])
    await page.goto(host.href)
    await page.setContent(
      `<iframe src="${new URL('/m/name-keychain', baseURL).href}" title="ScadBuddy"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
        style="position: fixed; left: 180px; top: 56px; width: 1100px; height: 664px; border: 0"></iframe>`,
    )
    const frame = page.frameLocator('iframe')
    await expect(frame.getByTestId('bbox-readout')).toContainText('64.1', { timeout: 15_000 })
    const canvas = frame.getByTestId('preview-canvas')
    const docked = await canvas.boundingBox()

    await frame.getByRole('button', { name: 'Full screen', exact: true }).click()
    await expect(frame.getByRole('button', { name: 'Exit full screen' })).toBeVisible()
    await expect
      .poll(() => canvas.boundingBox())
      .toEqual({ x: 180, y: 56, width: 1100, height: 664 })
    // Covered is not gone: Tab must still not reach the page under the stand-in.
    const app = page.frames().find((candidate) => candidate.url().includes('/m/name-keychain'))
    if (!app) throw new Error('the ScadBuddy frame is not loaded')
    const stops = await tabStops(page, app, 8)
    expect(stops).toContain('view')
    expect(stops.filter((stop) => stop !== 'view' && stop !== 'page')).toEqual([])

    // Tab may have carried the focus out to the page around the frame.
    await frame.getByRole('button', { name: 'Exit full screen' }).focus()
    await page.keyboard.press('Escape')
    await expect(frame.getByRole('button', { name: 'Full screen', exact: true })).toBeVisible()
    await expect.poll(() => canvas.boundingBox()).toEqual(docked)
  })

  test('attaches an SVG to a file parameter and renders with it (#204)', async ({ page }) => {
    await page.goto('/m/gridfinity-bin')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByRole('tab', { name: 'Features' }).click()

    const rendered = page.waitForRequest(
      (request) =>
        request.method() === 'POST' &&
        request.url().endsWith('/render') &&
        /^[0-9a-f]{64}$/.test(String(request.postDataJSON()?.inputs?.params?.label_art ?? '')),
    )
    await page.getByLabel('Label artwork').setInputFiles({
      name: 'heart.svg',
      mimeType: 'image/svg+xml',
      buffer: Buffer.from(
        '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><path d="M0 0H4V4Z"/></svg>',
      ),
    })

    await expect(page.getByText('heart.svg')).toBeVisible()
    await expect(page.getByRole('img', { name: 'Preview of heart.svg' })).toBeVisible()
    await rendered

    await page.getByRole('button', { name: 'Clear Label artwork' }).click()
    await expect(page.getByText('Drop a file here')).toBeVisible()
  })

  test('picks a sample the template ships for a file parameter', async ({ page }) => {
    await page.goto('/m/gridfinity-bin')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByRole('tab', { name: 'Features' }).click()

    const samples = page.getByTestId('samples-label_art')
    await expect(samples.getByRole('button')).toHaveCount(2)

    const rendered = page.waitForRequest(
      (request) =>
        request.method() === 'POST' &&
        request.url().endsWith('/render') &&
        request.postDataJSON()?.inputs?.params?.label_art === 'sample-heart.svg',
    )
    await samples.getByRole('button', { name: 'Use sample sample-heart.svg' }).click()

    const preview = page.getByRole('img', { name: 'Preview of sample-heart.svg' })
    await expect(preview).toBeVisible()
    // The thumbnail actually loaded from the sample route, not a broken image.
    await expect
      .poll(() => preview.evaluate((img) => (img as { naturalWidth: number }).naturalWidth))
      .toBeGreaterThan(0)
    await expect(page.getByText('Template sample')).toBeVisible()
    const request = await rendered
    const job = (await (await request.response())?.json()) as { job_id?: string } | undefined
    expect(job?.job_id).toBeTruthy()
  })

  test('shows the OpenSCAD log when a render fails', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    await page.getByRole('textbox', { name: 'Name on the tag' }).fill('boom')
    await expect(page.getByTestId('render-log')).toContainText('Compilation failed')
  })

  test("shows the notes a successful render's template echoed (#285)", async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await expect(page.getByTestId('render-notes')).toHaveCount(0)

    await page.getByRole('textbox', { name: 'Name on the tag' }).fill('Alexandra')
    const notes = page.getByRole('region', { name: 'Notes from the template' })
    await expect(notes).toContainText('text_size reduced from 14 to 9.5 mm')
    await expect(notes).toContainText('0.4 mm nozzle cannot print them cleanly')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await expect(page.getByTestId('render-log')).toHaveCount(0)
  })

  test("shows ScadBuddy's own job warnings on a successful render (#383)", async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await expect(page.getByTestId('render-warnings')).toHaveCount(0)

    await page.getByRole('textbox', { name: 'Name on the tag' }).fill('nopic')
    const warnings = page.getByRole('region', { name: 'Render warnings' })
    await expect(warnings).toContainText('OpenSCAD could not open pic.svg')
    await expect(warnings).toContainText('From ScadBuddy')
    await expect(page.getByTestId('render-notes')).toHaveCount(0)
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
  })

  test('shows a failed render the file it could not open (#408)', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    await page.getByRole('textbox', { name: 'Name on the tag' }).fill('nosvg')
    await expect(page.getByTestId('render-log')).toContainText('Current top level object is empty')
    const warnings = page.getByRole('region', { name: 'Render warnings' })
    await expect(warnings).toHaveText(/From ScadBuddy\s*OpenSCAD could not open pic\.svg/)
  })

  test('keeps the page when a preview fails to load, and loads the next one (#361)', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    const name = page.getByRole('textbox', { name: 'Name on the tag' })
    await name.fill('noglb')
    await expect(page.getByTestId('preview-failed')).toContainText('Could not load the preview.')
    // Only the viewer failed: the header, the parameters and the actions stay.
    await expect(page.getByRole('heading', { name: 'Name Keychain' })).toBeVisible()
    await expect(page.getByTestId('generate')).toBeVisible()

    await name.fill('Nova')
    await expect(page.getByTestId('preview-failed')).toHaveCount(0)
    await expect(page.getByTestId('preview-canvas')).toBeVisible()
    await expect(page.getByTestId('bbox-readout')).toContainText('46.7 × 37.2 × 6.8 mm')
  })
})

test.describe('font picker', () => {
  test.skip(
    !!process.env.E2E_BASE_URL,
    'msw-backed; the real stack is covered by real-backend.spec.ts',
  )

  test('searches Google Fonts, previews the name and puts the pick on the parameter', async ({
    page,
  }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    await page.getByRole('button', { name: 'Browse' }).click()
    const dialog = page.getByRole('dialog', { name: 'Choose a font' })

    // The preview defaults to the text the model will actually set.
    await expect(dialog.getByRole('textbox', { name: 'Sample text' })).toHaveValue('Reagan')

    await dialog.getByRole('searchbox', { name: 'Search fonts' }).fill('Pacifico')
    const row = dialog.getByRole('button', { name: /Pacifico/ })
    await expect(row).toBeVisible()
    await expect(row.locator('span').first()).toHaveCSS('font-family', /Pacifico/)

    await row.click()

    await expect(dialog).toBeHidden()
    await expect(page.getByRole('combobox', { name: 'Typeface' })).toHaveValue(
      'Pacifico:style=Regular',
    )
  })

  test('says so when a font cannot be downloaded', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()

    await page.getByRole('button', { name: 'Browse' }).click()
    const dialog = page.getByRole('dialog', { name: 'Choose a font' })
    await dialog.getByRole('searchbox', { name: 'Search fonts' }).fill('Playfair')
    await dialog.getByRole('button', { name: /Playfair Display/ }).click()

    await expect(dialog.getByRole('alert')).toContainText('could not be downloaded')
    await expect(dialog).toBeVisible()
  })
})
