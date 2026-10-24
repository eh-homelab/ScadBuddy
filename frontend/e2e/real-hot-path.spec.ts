import { expect, test, type APIRequestContext } from '@playwright/test'

/**
 * #3818 (plan 2026-10-18 Task 13, AC1 and AC2) — the native hot path against a real stack
 * (`e2e-stack/compose.yaml`): ScadBuddy with the S3 store, its own library and the native
 * slicer worker, a fake LAN printer (`e2e-stack/fake-printer`) and, as `bambuddy_url`, a
 * Bambuddy that fails and lists every call (`e2e-stack/bambuddy-recorder`). The walk is
 * the msw one's (`hot-path.spec.ts`), on a real OpenSCAD render, the real slicer's
 * progress, the trays of the fake printer's report, and its print to the end.
 *
 * `e2e-stack/run.sh` brings the stack up, seeds it (`seed.sh`) and runs this file with:
 *
 *   E2E_BASE_URL       the app (http://127.0.0.1:18080)
 *   E2E_HOT_PATH=1     this file's switch: without it (and E2E_BASE_URL) it skips
 *   E2E_HOT_PATH_MODE  `recorder` (bambuddy_url is the recorder, AC1) or `unset` (none, AC2)
 *   E2E_PRINTER_URL    the fake printer's control port (`GET /__received`)
 *   E2E_RECORDER_URL   the recorder (`GET /__recorded`), in recorder mode
 *
 * The recorder pass fails until #4629: with a URL set, the native dialog's analyzers and
 * the Prints page's queue, history stats and printer names still ask Bambuddy.
 *
 * It prints once per stack: the seed clears both fakes' lists, and a retry would find
 * the first attempt's print in them, so it never retries.
 */
const mode = process.env.E2E_HOT_PATH_MODE ?? 'recorder'
const printerUrl = process.env.E2E_PRINTER_URL ?? 'http://127.0.0.1:18081'
const recorderUrl = process.env.E2E_RECORDER_URL ?? 'http://127.0.0.1:18000'

interface Received {
  mqtt: { print?: { command?: string; ams_mapping?: number[]; md5?: string; url?: string } }[]
  ftps: { name: string; size: number; md5: string }[]
  refused: { url: string; reason: string }[]
}

/**
 * The fake printer's trays (`e2e-stack/fake-printer/report.json`) by the dialog's label,
 * with the global tray id `ams_mapping` names for each: AMS `k` tray `t` is `4k + t`
 * (OpenBambuAPI `mqtt.md`, `print.project_file`). AMS 0 tray 0 is the tagged one.
 */
const PINK = { label: 'PLA pink in AMS-A slot 1', tray: 0 }
const WHITE = { label: 'PLA white in AMS-A slot 2', tray: 1 }

async function recorded(request: APIRequestContext): Promise<unknown[]> {
  return (await (await request.get(`${recorderUrl}/__recorded`)).json()) as unknown[]
}

test.use({ trace: 'retain-on-failure' })

test.describe('native hot path (real stack)', () => {
  test.skip(
    !process.env.E2E_BASE_URL || process.env.E2E_HOT_PATH !== '1',
    'set E2E_BASE_URL and E2E_HOT_PATH=1: e2e-stack/run.sh does',
  )
  test.describe.configure({ retries: 0 })

  test(`renders, slices, prints on the LAN printer and follows it (bambuddy_url ${mode})`, async ({
    page,
    request,
  }) => {
    test.setTimeout(600_000)

    await page.goto('/')
    await page.getByRole('link', { name: /Name Keychain/ }).first().click()
    // A real OpenSCAD render, then the output it saves.
    await expect(page.getByTestId('bbox-readout')).toContainText('mm', { timeout: 180_000 })
    const generate = page.getByTestId('generate')
    await expect(generate).toBeEnabled({ timeout: 180_000 })
    await generate.click()
    await expect(page.getByText(/^Saved /)).toBeVisible({ timeout: 180_000 })
    // AC1, so far: the render and the output it saved asked Bambuddy nothing. Soft, so a
    // call here is named and the walk still shows what else asks.
    if (mode === 'recorder') expect.soft(await recorded(request), 'Bambuddy calls before the dialog').toEqual([])

    await page.getByTestId('print').click()
    const dialog = page.getByRole('dialog', { name: 'Print' })
    await expect(dialog.getByText('ScadBuddy slices it and sends it to the printer over the LAN.')).toBeVisible({
      timeout: 60_000,
    })

    // The trays are the fake printer's report. Slot 2 (#FF1493) is matched to the pink
    // tray; slot 1 (#0047BB) matches none, so it is chosen by hand.
    const first = dialog.getByTestId('filament-slot-1')
    const second = dialog.getByTestId('filament-slot-2')
    await expect(second.getByRole('radio', { name: new RegExp(PINK.label) })).toBeChecked({ timeout: 60_000 })
    // The native slicer worker's progress bar: watched from before the choice that
    // starts the slice, so a quick slice cannot finish before it is looked for.
    const progress = page.getByTestId('slice-progress').waitFor({ timeout: 300_000 })
    await first.getByRole('radio', { name: new RegExp(WHITE.label) }).click()
    await progress
    await expect(dialog.getByTestId('slice-time')).toHaveText(/\d/, { timeout: 300_000 })

    // The tagged tray holds the recorder's spool 2, linked by RFID from the refresh the
    // seed ran: Bambuddy's record over it, read from Postgres, not from Bambuddy. With no
    // Bambuddy there is no record.
    if (mode === 'recorder') {
      await expect(second).toContainText('Bambuddy: Bambu Lab Hot Pink')
    } else {
      await expect(second).not.toContainText('Bambuddy:')
    }

    await dialog.getByRole('button', { name: 'Print', exact: true }).click()
    await expect(dialog.getByTestId('queued-items')).toContainText('over the LAN', { timeout: 120_000 })
    await dialog.getByRole('button', { name: 'Done' }).click()

    await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Prints' }).click()
    const now = page.getByTestId('now-printing')
    await expect(now.getByText('H2C', { exact: true })).toBeVisible()
    await expect(now).toContainText(/finished/i, { timeout: 180_000 })

    // What the printer was sent: one project_file naming the trays chosen, and one upload
    // whose md5 is the command's.
    const received = (await (await request.get(`${printerUrl}/__received`)).json()) as Received
    const starts = received.mqtt.filter((message) => message.print?.command === 'project_file')
    expect(starts).toHaveLength(1)
    const start = starts[0]?.print
    expect(start, 'the project_file command').toBeDefined()
    if (!start) return
    expect(start.ams_mapping).toEqual([WHITE.tray, PINK.tray])
    expect(received.ftps).toHaveLength(1)
    expect(received.ftps[0].md5).toBe(start.md5?.toLowerCase())
    expect(received.refused).toEqual([])

    // AC1: nothing on the hot path asked Bambuddy.
    if (mode === 'recorder') {
      expect(await recorded(request)).toEqual([])
    }
  })
})
