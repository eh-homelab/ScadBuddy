import { createHash } from 'node:crypto'
import { expect, test } from '@playwright/test'

/**
 * The real stack: a real OpenSCAD behind the real FastAPI app, no msw anywhere.
 *
 * Gated on `E2E_BASE_URL` because it needs that stack running — see the README. Run it
 * against a container built from `openscad/openscad:dev` plus
 * `fonts-lobster fonts-lobstertwo fonts-dejavu fonts-noto-core`: without Lobster Two the
 * keychain silently falls back to DejaVu and measures ~107 × 32 instead (spec §11).
 */
test.describe('real backend', () => {
  test.skip(!process.env.E2E_BASE_URL, 'set E2E_BASE_URL to the running stack')
  test.describe.configure({ mode: 'serial' })

  test('renders, generates and downloads a two-extruder 3MF', async ({ page }, testInfo) => {
    test.setTimeout(180_000)

    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Models' })).toBeVisible()
    await page.getByRole('link', { name: /Name Keychain/ }).click()

    // The first render happens without being asked, and it is a real OpenSCAD run.
    const bbox = page.getByTestId('bbox-readout')
    await expect(bbox).toContainText('mm', { timeout: 120_000 })
    await expect(page.getByTestId('preview-canvas')).toBeVisible()
    const defaultBbox = await bbox.textContent()

    // The caption comes from the model's own `.scad` comment, not its name.
    await page.getByRole('textbox', { name: /Word to put on the keychain/ }).fill('Elan')
    // A four-letter name is narrower than the six-letter default, so the readout must
    // actually change — not merely still be present.
    await expect(bbox).not.toHaveText(defaultBbox ?? '', { timeout: 120_000 })
    await expect(page.getByText('Rendering')).toBeHidden({ timeout: 120_000 })
    await testInfo.attach('preview-elan.png', {
      body: await page.screenshot({ fullPage: true }),
      contentType: 'image/png',
    })

    const generate = page.getByTestId('generate')
    await expect(generate).toBeEnabled({ timeout: 120_000 })
    await generate.click()
    await expect(page.getByText(/^Saved /)).toBeVisible({ timeout: 60_000 })

    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Download 3MF' }).click()
    const file = await (await download).path()
    expect(file).toBeTruthy()

    const bambu = await readBambu3mf(file as string)
    // One plate object holding one body per colour, each with its own extruder — the
    // shape Bambu Studio maps to filaments without any painting (spec §2).
    expect(bambu.parts).toEqual(['Color 1', 'Color 2'])
    expect(bambu.extruders).toEqual([1, 2])
    expect(bambu.meshes).toEqual(['3D/Objects/object_1.model', '3D/Objects/object_2.model'])
  })

  /**
   * Issue #82's acceptance, end to end: Pacifico is NOT one of the image's font
   * packages, so this only passes if the picker downloaded it onto the data volume and
   * fontconfig picked it up before the next render. The bounding box is the evidence —
   * a missing family does not error, it silently substitutes, and a substitution would
   * leave the measurement where Lobster Two put it.
   *
   * Needs outbound HTTPS to fonts.google.com; skipped without it rather than failing,
   * since an air-gapped stack is a supported way to run ScadBuddy.
   */
  test('installs Pacifico on demand and renders the keychain in it', async ({ page }, testInfo) => {
    test.setTimeout(240_000)
    test.skip(process.env.E2E_OFFLINE === '1', 'no outbound network to Google Fonts')

    await page.goto('/m/name-keychain')
    const bbox = page.getByTestId('bbox-readout')
    await expect(bbox).toContainText('mm', { timeout: 120_000 })
    const beforeBbox = await bbox.textContent()

    await page.getByRole('button', { name: 'Browse' }).click()
    const dialog = page.getByRole('dialog', { name: 'Choose a font' })
    await dialog.getByRole('searchbox', { name: 'Search fonts' }).fill('Pacifico')
    const row = dialog.getByRole('button', { name: /^Pacifico/ })
    await expect(row).toBeVisible({ timeout: 60_000 })
    await row.click()
    await expect(dialog).toBeHidden({ timeout: 120_000 })

    await expect(page.getByRole('combobox', { name: /Typeface/ })).toHaveValue(/^Pacifico/)
    await expect(bbox).not.toHaveText(beforeBbox ?? '', { timeout: 120_000 })
    await expect(page.getByText('Rendering')).toBeHidden({ timeout: 120_000 })
    await testInfo.attach('preview-pacifico.png', {
      body: await page.screenshot({ fullPage: true }),
      contentType: 'image/png',
    })
  })
})

/**
 * A 3MF is a zip. Rather than pull in a zip library for one assertion, the central
 * directory is walked directly and the stored entries are read; Bambu's writer stores
 * the model parts deflated, so only their names and the `extruder` assignments in
 * `Metadata/model_settings.config` are needed — and that file is small enough to
 * inflate with zlib from node's own standard library.
 */
async function readBambu3mf(
  path: string,
): Promise<{ parts: string[]; extruders: number[]; meshes: string[] }> {
  const { readFile } = await import('node:fs/promises')
  const { inflateRawSync } = await import('node:zlib')
  const buffer = await readFile(path)

  const entries = new Map<string, Buffer>()
  // End of central directory: scan back for its signature.
  let eocd = buffer.length - 22
  while (eocd >= 0 && buffer.readUInt32LE(eocd) !== 0x06054b50) eocd -= 1
  expect(eocd, 'the download is not a zip').toBeGreaterThanOrEqual(0)

  let offset = buffer.readUInt32LE(eocd + 16)
  const count = buffer.readUInt16LE(eocd + 10)
  for (let i = 0; i < count; i += 1) {
    const method = buffer.readUInt16LE(offset + 10)
    const compressed = buffer.readUInt32LE(offset + 20)
    const nameLength = buffer.readUInt16LE(offset + 28)
    const extraLength = buffer.readUInt16LE(offset + 30)
    const commentLength = buffer.readUInt16LE(offset + 32)
    const local = buffer.readUInt32LE(offset + 42)
    const name = buffer.toString('utf8', offset + 46, offset + 46 + nameLength)

    const localName = buffer.readUInt16LE(local + 26)
    const localExtra = buffer.readUInt16LE(local + 28)
    const start = local + 30 + localName + localExtra
    const raw = buffer.subarray(start, start + compressed)
    entries.set(name, method === 8 ? inflateRawSync(raw) : Buffer.from(raw))

    offset += 46 + nameLength + extraLength + commentLength
  }

  const settings = entries.get('Metadata/model_settings.config')?.toString('utf8') ?? ''
  // Only the <part> blocks, not the object's own inherited extruder metadata.
  const partBlocks = [...settings.matchAll(/<part\b[\s\S]*?<\/part>/g)].map((m) => m[0])
  const parts = partBlocks.map((block) => /key="name"\s+value="([^"]*)"/.exec(block)?.[1] ?? '')
  const extruders = partBlocks.map((block) =>
    Number(/key="extruder"\s+value="(\d+)"/.exec(block)?.[1] ?? 0),
  )
  const meshes = [...entries.keys()].filter((name) => name.startsWith('3D/Objects/')).sort()
  // Hashed so a failure report names the file that was actually inspected.
  test.info().annotations.push({
    type: 'sha256',
    description: createHash('sha256').update(buffer).digest('hex').slice(0, 16),
  })
  return { parts, extruders, meshes }
}
