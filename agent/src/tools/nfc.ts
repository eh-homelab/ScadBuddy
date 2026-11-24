import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, type ToolContext } from './registry.js'

// #7273 G4: an agent reads or writes a tag through the person's phone. The tool queues an
// NFC job (backend filament/tags.py `create_job`); the /filament/scan page shows it, the
// person taps Arm and holds a tag, and the page posts what the tag held.

const ENDED = new Set(['done', 'failed', 'expired'])
const SOURCE = 'NDEF records read from a physical tag, which anyone holding the tag could have written'
const HOW =
  "The user must have ScadBuddy's /filament/scan page open in Chrome on Android. It shows the job; the user " +
  'taps Arm and holds a tag. Records are NDEF records with data as hex. The write replaces every record on the ' +
  'tag, after a backup the user can restore.'

const TIMEOUT = z.number().int().min(1).max(600).default(600).describe('How long to wait for the user, in seconds')
const RECORD = z.object({
  record_type: z.string().min(1).max(255).regex(/^[A-Za-z0-9:._/+-]+$/).describe('e.g. "mime", "url", "text"'),
  media_type: z.string().max(127).nullish().describe('The MIME type of a "mime" record'),
  id: z.string().max(255).nullish(),
  encoding: z.string().max(32).nullish(),
  lang: z.string().max(35).nullish(),
  data_hex: z.string().max(1776).regex(/^([0-9a-fA-F]{2})*$/).describe('The payload as hex'),
})

type Job = { id: string; state: string; result?: Record<string, unknown> | null }

/** Queue the job, then wait as activity_watch does until it ends, the call is aborted or the time is up. */
async function runJob(ctx: ToolContext, body: { kind: 'read' | 'write'; ttl_s: number; records?: z.infer<typeof RECORD>[] }) {
  const deadline = Date.now() + body.ttl_s * 1000
  const made: Job = await ok(ctx.backend.POST('/api/v1/filament/nfc-jobs', { body }), `queue an NFC ${body.kind}`)
  const job_id = made.id
  let found = made
  while (!ENDED.has(found.state) && Date.now() < deadline && !ctx.signal.aborted) {
    try {
      await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
    } catch (err) {
      if (ctx.signal.aborted) break
      throw err
    }
    found = await ok(
      ctx.backend.GET('/api/v1/filament/nfc-jobs/{job_id}', { params: { path: { job_id } } }),
      `read NFC job ${job_id}`,
    )
    await ctx.progress(0, undefined, found.state)
  }
  if (!ENDED.has(found.state) && !ctx.signal.aborted) return json({ job_id, state: 'expired' })
  return json({ job_id, state: found.state, result: found.result ?? null })
}

export const nfcTools = [
  defineTool({
    name: 'nfc_read',
    label: 'Read an NFC tag',
    description:
      `Read any NFC tag through the user's phone, and wait up to timeout_s for it. ${HOW} Any tag, not only a ` +
      "spool's. The result is the tag's UID, its records and what ScadBuddy makes of them (the spool that holds " +
      'it, or what to do next).',
    input: z.object({ timeout_s: TIMEOUT }),
    // #7273 R8: it makes a job row (and its result records the tag as seen); the tap is the person's own act.
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/nfc-jobs', 'GET /api/v1/filament/nfc-jobs/{job_id}'],
    waitsMs: ({ timeout_s }) => timeout_s * 1000,
    handler: async ({ timeout_s }, ctx) => runJob(ctx, { kind: 'read', ttl_s: timeout_s }),
  }),
  defineTool({
    name: 'nfc_write',
    label: 'Write an NFC tag',
    description:
      `Write NDEF records to an NFC tag through the user's phone, and wait up to timeout_s for it. ${HOW} The ` +
      'job is done only when the tag reads back as the records sent; else it fails, with what the tag holds.',
    input: z.object({ records: z.array(RECORD).min(1).max(16), timeout_s: TIMEOUT }),
    risk: 'outward',
    // docs/superpowers/specs/2026-11-23-web-nfc-tags-design.md, Constraints: "The person's
    // arm-and-tap on the page is its approval, so no pending action is needed" (#7273 R8).
    approval: 'none',
    source: SOURCE,
    routes: ['POST /api/v1/filament/nfc-jobs', 'GET /api/v1/filament/nfc-jobs/{job_id}'],
    waitsMs: ({ timeout_s }) => timeout_s * 1000,
    handler: async ({ records, timeout_s }, ctx) => runJob(ctx, { kind: 'write', records, ttl_s: timeout_s }),
  }),
]
