import { chromium } from '/home/elan/repos/eh-homelab/ScadBuddy/frontend/node_modules/.pnpm/@playwright+test@1.63.0/node_modules/@playwright/test/index.mjs'
import { installGuard } from './guard.mjs'
import fs from 'node:fs'
const OUT = 'f542c216a3104334b252d4067e64ea11'
const SLUG = 'hanukkah-play-menorah-with-storage-base-p1s-13-plates-v1-2-pretendplaylab-20261002'
const plates = process.argv.slice(2).map(Number)
const results = { mode: process.env.MODE || 'single', output_id: OUT, slug: SLUG, started: new Date().toISOString(), plates: [] }
const b = await chromium.launch()
for (const plate of plates) {
  const log = { allowed: [], aborted: [] }
  const reads = []
  const ctx = await b.newContext({ viewport: { width: 1600, height: 1400 }, ignoreHTTPSErrors: true })
  await installGuard(ctx, log)
  const p = await ctx.newPage()
  p.on('response', async (r) => {
    const u = new URL(r.url())
    if (/\/preview-slice(s\/\d+)?$|\/commands\/op-preview_slice-|\/operations\//.test(u.pathname)) {
      try { reads.push({ t: Date.now(), path: u.pathname + u.search, status: r.status(), body: await r.json() }) } catch {}
    }
  })
  await p.goto(`https://scadbuddy.internal.nullreference.io/m/${SLUG}/history`, { waitUntil: 'networkidle' })
  await p.getByRole('button', { name: 'Print Hanukkah Menorah Play Set — Sari (by colour)', exact: true }).click()
  const d = p.getByRole('dialog')
  await d.getByRole('button', { name: 'None', exact: true }).waitFor()
  await p.waitForTimeout(3000)
  const MODE = process.env.MODE || 'single'
  if (MODE === 'single') await d.getByRole('button', { name: 'None', exact: true }).click()
  // The plate rows follow None: one toggle per plate, in order.
  const none = d.getByRole('button', { name: 'None', exact: true })
  const rows = d.locator('button').filter({ has: p.locator('xpath=.') })
  const allBtns = await d.locator('button').all()
  let idx = -1
  for (let i = 0; i < allBtns.length; i++) if ((await allBtns[i].innerText()).trim() === 'None') { idx = i; break }
  const toggle = allBtns[idx + plate]
  const label = (await toggle.innerText()).trim()
  await toggle.click()
  if (MODE === 'all') { await p.waitForTimeout(800); await toggle.click() }
  // Wait for the slice to finish: a finished read, or an error, up to 8 minutes.
  const t0 = Date.now()
  let final = null
  while (Date.now() - t0 < 8 * 60 * 1000) {
    const mine = log.allowed.filter((a) => a.path.endsWith('/preview-slice') && a.plate_id === plate).at(-1)
    const since = mine ? Date.parse(mine.at) : Infinity
    final = reads.filter((r) => r.t > since && r.body && ['completed', 'failed', 'cancelled'].includes(r.body.status)).at(-1)
    if (final) break
    const t = await d.innerText().catch(() => '')
    if (Date.now() - t0 > 60000 && /could not reach its server|The background slice could not run/.test(t.slice(t.lastIndexOf('\nSlice\n')))) break
    await p.waitForTimeout(3000)
  }
  await p.waitForTimeout(2500)
  const text = await d.innerText()
  const previewing = (text.match(/Previewing\n([^\n]*)/) || [])[1] ?? null
  const sliceSection = text.slice(text.lastIndexOf('\nSlice\n'))
  await p.screenshot({ path: `plate-${plate}.png`, fullPage: true })
  const shot = await d.screenshot({ path: `plate-${plate}-dialog.png` }).catch(() => null)
  const fb = final?.body ?? {}
  const entry = {
    plate, toggle_label: label, previewing, elapsed_s: Math.round((Date.now() - t0) / 1000),
    slice: final ? { job_id: fb.job_id, status: fb.status, failure: fb.failure ?? null, plate_id: fb.plate_id ?? null,
      prediction_s: fb.print_time_seconds ?? fb.prediction ?? fb.time_seconds ?? null, weight_g: fb.weight_grams ?? fb.weight ?? null,
      keys: Object.keys(fb) } : null,
    slice_section_text: sliceSection.slice(0, 1200),
    allowed: log.allowed, aborted: log.aborted,
  }
  fs.writeFileSync(`plate-${plate}-reads.json`, JSON.stringify(reads, null, 1))
  results.plates.push(entry)
  console.log(JSON.stringify(entry, null, 1))
  await ctx.close()
}
results.finished = new Date().toISOString()
fs.writeFileSync('results.json', JSON.stringify(results, null, 1))
await b.close()
