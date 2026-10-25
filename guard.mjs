// Write guard: every GET/HEAD passes. The only non-GETs allowed are the print dialog's
// background slice (POST /api/v1/print/outputs/{id}/preview-slice) and the read-only check
// the dialog requires before it slices (POST /api/v1/print/outputs/{id}/check). All else aborts.
export const ALLOWED_POST = [
  /^\/api\/v1\/print\/outputs\/[0-9a-f]{32}\/preview-slice$/,
  /^\/api\/v1\/print\/outputs\/[0-9a-f]{32}\/check$/,
  // The 202 follow of that start: waits on the preview_slice operation's Update and reads its answer.
  /^\/api\/v1\/commands\/op-preview_slice-[0-9a-f]{64}\/updates\/[0-9a-f]{32}$/,
]
export async function installGuard(context, log) {
  await context.route('**/*', async (route) => {
    const req = route.request()
    const m = req.method()
    const u = new URL(req.url())
    if (m === 'GET' || m === 'HEAD') return route.continue()
    if (m === 'POST' && u.search === '' && ALLOWED_POST.some((r) => r.test(u.pathname))) {
      let body = null
      try { body = req.postDataJSON() } catch {}
      log.allowed.push({ method: m, path: u.pathname, plate_id: body?.plate_id ?? null, plates: body?.plates ?? null, body, at: new Date().toISOString() })
      return route.continue()
    }
    log.aborted.push({ method: m, path: u.pathname + u.search, at: new Date().toISOString() })
    console.log('ABORTED', m, u.pathname + u.search)
    return route.abort('blockedbyclient')
  })
}
