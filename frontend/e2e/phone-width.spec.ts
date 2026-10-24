import { expect, test, type Locator, type Page } from '@playwright/test'

// #362 — the customize page at a phone's width. msw-backed: the templates are fixtures.
test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')

test.use({ viewport: { width: 390, height: 844 } })

/**
 * Every visible control on the page, open shadow roots included (a template UI mounts
 * in one), that does not lie inside the screen: the page cannot scroll sideways, so
 * those are out of reach.
 */
async function offscreen(page: Page): Promise<string[]> {
  return (await page.evaluate(`(() => {
    const width = document.documentElement.clientWidth
    const out = []
    const visit = (root) => {
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) visit(el.shadowRoot)
        if (!el.matches('button, a[href], input, select, textarea, [role="tab"]')) continue
        if (el.closest('[aria-hidden="true"], [hidden]')) continue
        const box = el.getBoundingClientRect()
        if (box.width === 0 || box.height === 0) continue
        const style = getComputedStyle(el)
        if (style.visibility === 'hidden') continue
        // Visually hidden until focused (sr-only: the skip link, #2454): its 1px box sits at
        // -1px, and no one sees it there.
        if (style.clipPath === 'inset(50%)' || style.clip === 'rect(0px, 0px, 0px, 0px)') continue
        // Clipped on purpose by a scroller of its own (a scrolling tab row) is reachable.
        let clipped = false
        for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
          const s = getComputedStyle(up)
          if (/(auto|scroll)/.test(s.overflowX) && up.scrollWidth > up.clientWidth + 1) { clipped = true; break }
        }
        if (clipped) continue
        if (box.left < -0.5 || box.right > width + 0.5) {
          const label = el.getAttribute('aria-label') || el.textContent.trim().slice(0, 30) || el.getAttribute('name') || el.tagName
          out.push(label + ' ' + Math.round(box.left) + '..' + Math.round(box.right))
        }
      }
    }
    visit(document)
    return out
  })()`)) as string[]
}

async function pageWidth(page: Page): Promise<number> {
  return Number(await page.evaluate('document.documentElement.scrollWidth'))
}

/**
 * Every vertical scroller (`overflow-y-auto`, a page's own scroll area among them) that also
 * scrolls sideways. CSS gives such a box `overflow-x: auto` too, so content wider than it
 * pans the whole page, and `offscreen` reads it as a deliberate sideways scroller (#1036).
 */
async function sidewaysScrollers(page: Page): Promise<string[]> {
  return (await page.evaluate(`[...document.querySelectorAll('[class~="overflow-y-auto"]')]
    .filter((el) => el.scrollWidth > el.clientWidth + 1)
    .map((el) => el.className + ' ' + el.scrollWidth + '>' + el.clientWidth)`)) as string[]
}

test.describe('customize page at 390 px (#362)', () => {
  test('a generated form keeps every control on screen', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    expect(await pageWidth(page)).toBeLessThanOrEqual(390)
    expect(await offscreen(page)).toEqual([])
    // Focused, the skip link is shown, and on screen too.
    await page.getByRole('link', { name: 'Skip to main content' }).focus()
    expect(await offscreen(page)).toEqual([])
  })

  test('after Generate, Print and the downloads are on screen', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    await page.getByTestId('generate').click()
    await expect(page.getByText(/^Saved /)).toBeVisible()
    // #1741 — behind More on a phone, which keeps the action bar to one row.
    await page.getByTestId('more-actions').click()
    await expect(page.getByTestId('print')).toBeInViewport()
    expect(await pageWidth(page)).toBeLessThanOrEqual(390)
    expect(await offscreen(page)).toEqual([])
  })

  test("a template's own panel UI fits the screen", async ({ page }) => {
    await page.goto('/m/builtin%3Amaze-puzzle')
    await expect(page.getByTestId('ui-origin')).toBeVisible()
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    expect(await pageWidth(page)).toBeLessThanOrEqual(390)
    expect(await offscreen(page)).toEqual([])
  })
})

test.describe('model lists at 390 px (#1036)', () => {
  test('Versions keeps a long commit message inside its card', async ({ page }) => {
    await page.goto('/m/name-keychain')
    await expect(page.getByTestId('bbox-readout')).toBeVisible()
    // The message an agent or an editor writes, not the short default: the list sized to it.
    await page.evaluate(`(async () => {
      const url = '/api/v1/models/name-keychain/source'
      const source = await (await fetch(url)).text()
      const message = 'Thicken the raised text to 2 mm so the thin strokes of the letters survive printing'
      const put = await fetch(url, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source, message }),
      })
      if (!put.ok) throw new Error('save failed: ' + put.status)
    })()`)
    await page.getByRole('link', { name: 'Versions' }).click()
    const versions = page.getByTestId('versions')
    await expect(versions.locator('li').first()).toContainText('Thicken the raised text')
    expect(await sidewaysScrollers(page)).toEqual([])
    expect((await versions.boundingBox())!.width).toBeLessThanOrEqual(390)
    await expect(versions.getByText('current', { exact: true })).toBeInViewport({ ratio: 1 })
    // Its options are whole commit messages, so the Compare select is as wide as the longest.
    const changes = (await page.locator('section', { has: page.getByRole('heading', { name: 'Changes' }) }).boundingBox())!
    const compare = (await page.getByLabel('Compare with').boundingBox())!
    expect(compare.x + compare.width).toBeLessThanOrEqual(changes.x + changes.width + 0.5)
  })

  test('History keeps a changed value beside its long caption on screen', async ({ page }) => {
    await page.goto('/m/name-keychain/history')
    const value = page.locator('dd', { hasText: /^2\b/ }).first()
    await expect(value).toBeVisible()
    expect(await sidewaysScrollers(page)).toEqual([])
    await expect(value).toBeInViewport({ ratio: 1 })
    expect((await value.boundingBox())!.width).toBeGreaterThan(0)
  })
})

/**
 * #4491 — every pair of a History card's parts (picture, checkbox, colour chips, name, age,
 * the size line's facts, an arranged output's plates, a rename's error and each action) whose
 * boxes overlap, and every part outside its card.
 */
/** How many lines the element's text runs to, from its text's client rects. */
async function textLines(line: Locator): Promise<number> {
  // Typed by hand: this file is checked without the DOM lib.
  type Lined = {
    ownerDocument: {
      createRange(): { selectNodeContents(node: unknown): void; getClientRects(): ArrayLike<{ top: number; height: number }> }
    }
  }
  return line.evaluate((el: Lined) => {
    const range = el.ownerDocument.createRange()
    range.selectNodeContents(el)
    const tops = new Set<number>()
    for (const rect of Array.from(range.getClientRects())) if (rect.height > 0) tops.add(Math.round(rect.top))
    return tops.size
  })
}

async function cardOverlaps(page: Page): Promise<string[]> {
  return (await page.evaluate(`(() => {
    const out = []
    for (const card of document.querySelectorAll('li:has([data-testid="output-actions"])')) {
      const summary = card.querySelector('[data-testid="output-summary"]')
      const facts = summary && summary.querySelector('[data-testid="output-facts"]')
      // A renamed hook would otherwise surface as a TypeError on null from inside evaluate.
      if (!summary || !summary.firstElementChild || !facts) {
        out.push('a card without ' + (summary ? 'its size line (output-facts)' : 'output-summary'))
        continue
      }
      const parts = [
        ...card.querySelectorAll('[aria-label^="Preview "], [aria-label^="No preview of "]'),
        ...summary.firstElementChild.querySelectorAll(
          'button[role="checkbox"], [role="img"], [data-testid="output-name"], [data-testid="output-age"], form > *',
        ),
        ...facts.children,
        // #4523 — the arranged plates and a failed rename's alert, below the size line.
        ...summary.querySelectorAll('[data-testid="output-plates"], [data-testid="output-rename-error"]'),
        ...card.querySelector('[data-testid="output-actions"]').children,
      ]
      const name = (el) => el.getAttribute('aria-label') || el.textContent.trim().slice(0, 24) || el.tagName
      const frame = card.firstElementChild.getBoundingClientRect()
      const boxes = parts.map((el) => [el, el.getBoundingClientRect()]).filter(([, b]) => b.width > 0 && b.height > 0)
      for (const [el, b] of boxes) {
        if (b.left < frame.left - 0.5 || b.right > frame.right + 0.5) out.push(name(el) + ' outside its card')
      }
      for (let i = 0; i < boxes.length; i++) {
        for (let j = i + 1; j < boxes.length; j++) {
          const [a, p] = boxes[i]
          const [c, q] = boxes[j]
          if (a.contains(c) || c.contains(a)) continue
          const x = Math.min(p.right, q.right) - Math.max(p.left, q.left)
          const y = Math.min(p.bottom, q.bottom) - Math.max(p.top, q.top)
          if (x > 0.5 && y > 0.5) out.push(name(a) + ' over ' + name(c))
        }
      }
    }
    return out
  })()`)) as string[]
}

test.describe('History cards at phone widths (#4491)', () => {
  for (const width of [320, 375, 390, 430]) {
    test(`nothing on a card overlaps at ${width} px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 })
      await page.goto('/m/name-keychain/history')
      await expect(page.getByRole('button', { name: 'Print Reagan' })).toBeVisible()
      // The report's cards: a long name, and an output in six colours.
      const long = 'Coach Alan trophy v2 for the end of season dinner'
      await page.evaluate(`(async () => {
        const json = { 'Content-Type': 'application/json' }
        const outputs = await (await fetch('/api/v1/models/name-keychain/outputs')).json()
        const reagan = outputs.find((o) => o.name === 'Reagan')
        const renamed = await fetch('/api/v1/outputs/' + reagan.id, {
          method: 'PATCH',
          headers: json,
          body: JSON.stringify({ name: ${JSON.stringify(long)} }),
        })
        if (!renamed.ok) throw new Error('rename failed: ' + renamed.status)
        const colours = ['#111111', '#EEEEEE', '#E8532F', '#1B6CA8', '#F2A93B', '#2E7D5B']
        const arranged = await fetch('/api/v1/outputs/arrange', {
          method: 'POST',
          headers: json,
          body: JSON.stringify({ objects: [{ output_id: reagan.id, count: 1 }], colours }),
        })
        const job = await arranged.json()
        const saved = await fetch('/api/v1/models/name-keychain/outputs', {
          method: 'POST',
          headers: json,
          body: JSON.stringify({ job_id: job.id, name: 'Six colours' }),
        })
        if (!saved.ok) throw new Error('save failed: ' + saved.status)
      })()`)
      // Away and back client-side, so the list is read again and the mock keeps its state
      // (the model's tabs are behind a menu at 320 px).
      await page.evaluate(`(() => {
        history.pushState(null, '', '/m/name-keychain/versions')
        dispatchEvent(new PopStateEvent('popstate'))
      })()`)
      await expect(page.getByRole('button', { name: 'Print Reagan' })).toBeHidden()
      await page.goBack()
      await expect(page.getByRole('button', { name: `Print ${long}` })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Print Six colours' })).toBeVisible()

      expect(await sidewaysScrollers(page)).toEqual([])
      expect(await cardOverlaps(page)).toEqual([])
      // The long name truncates to one line, its whole text in the title; the age and the
      // library copy each stay on one line. Lines of text, counted from the text's own
      // boxes: the library copy is a 40 px tap target, taller than its one line.
      for (const line of [
        page.getByTitle(long),
        page.getByTestId('output-age').last(),
        page.getByText(/^in library #/).first(),
      ]) {
        expect(await textLines(line)).toBe(1)
      }
      // Every action a finger has to hit is 40 px tall, on every card: an arranged one has
      // "Arranged from …" where the others have Edit.
      const rows = await page.getByTestId('output-actions').all()
      expect(rows.length).toBeGreaterThanOrEqual(2)
      for (const row of rows) {
        // Buttons and links only: "Arranged from …" is a plain span, not a target.
        for (const action of await row.locator('button, a').all()) {
          expect((await action.boundingBox())!.height).toBeGreaterThanOrEqual(40)
        }
      }
      // #4524 — and the size line's links: Last print, queued #… and in library #…. On every
      // card, as the actions above: no one card has all three, so page-wide is the point.
      const facts = await page.getByTestId('output-facts').locator('a').all()
      expect(facts.length).toBeGreaterThanOrEqual(2)
      for (const link of facts) {
        expect((await link.boundingBox())!.height).toBeGreaterThanOrEqual(40)
      }

      // Rename's field and its two buttons fit the summary's 10rem: nothing overlaps or
      // leaves the card, and the field is wide enough to type in.
      await page.getByRole('button', { name: `More for ${long}` }).click()
      await page.getByRole('menuitem', { name: `Rename ${long}` }).click()
      const field = page.getByRole('textbox', { name: `Name for ${long}` })
      await expect(field).toBeVisible()
      expect(await cardOverlaps(page)).toEqual([])
      expect((await field.boundingBox())!.width).toBeGreaterThanOrEqual(64)

      // #4523 — a rename that fails says why under the name, inside the card: the output
      // is gone from the mock's store, so the save is a 404.
      await page.evaluate(`(async () => {
        const outputs = await (await fetch('/api/v1/models/name-keychain/outputs')).json()
        const gone = outputs.find((o) => o.name === ${JSON.stringify(long)})
        const deleted = await fetch('/api/v1/outputs/' + gone.id, { method: 'DELETE' })
        if (!deleted.ok) throw new Error('delete failed: ' + deleted.status)
      })()`)
      await page.getByRole('button', { name: 'Save' }).click()
      await expect(page.getByTestId('output-rename-error')).toHaveText('Output not found')
      expect(await cardOverlaps(page)).toEqual([])
    })
  }
})
