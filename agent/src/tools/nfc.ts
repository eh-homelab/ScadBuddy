import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, ToolError, type ToolContext } from './registry.js'
import { withoutBackground } from './sessions.js'

// #7273 G4: an agent reads or writes a tag through the person's phone. The tool queues an
// NFC job (backend filament/tags.py `create_job`); the /scan page shows it, the
// person taps Arm and holds a tag, and the page posts what the tag held.

const ENDED = new Set(['done', 'failed', 'expired', 'cancelled'])
const SOURCE = 'NDEF records read from a physical tag, which anyone holding the tag could have written'
const HOW =
  "The user must have ScadBuddy's /scan page (Scan in the nav; /filament/scan redirects there) open in Chrome on Android. It shows the job; the user " +
  'taps Arm and holds a tag. Records are NDEF records with data as hex. The write replaces every record on the ' +
  'tag, after a backup the user can restore.'

const WAIT =
  'It waits up to timeout_s for the user to arm the job, then the armed run has 60 seconds for the tap.'

const TIMEOUT = z.number().int().min(1).max(600).default(600).describe('How long to wait for the user, in seconds')
const RECORD = z.object({
  record_type: z.string().min(1).max(255).regex(/^[A-Za-z0-9:._/+-]+$/).describe('e.g. "mime", "url", "text"'),
  media_type: z.string().max(127).nullish().describe('The MIME type of a "mime" record'),
  id: z.string().max(255).nullish(),
  encoding: z.string().max(32).nullish(),
  lang: z.string().max(35).nullish(),
  data_hex: z.string().max(1776).regex(/^([0-9a-fA-F]{2})*$/).describe('The payload as hex'),
})

const ROUTES = [
  'POST /api/v1/filament/nfc-jobs',
  'GET /api/v1/filament/nfc-jobs/{job_id}',
  'POST /api/v1/filament/nfc-jobs/{job_id}/cancel',
] as const

type Job = { id: string; state: string; expires_at: string; write_begun?: boolean; result?: Record<string, unknown> | null }

/** How long an armed run has: the backend's `ARM_SECONDS` (filament/tags.py), which arming sets `expires_at` to. */
const ARM_MS = 60_000
/** How long a begun write is waited for: the backend's `BACKUP_TTL`, after which it ends `failed` `no_read_back`. */
const BACKUP_TTL_MS = 10 * 60_000
/** How long past an armed job's `expires_at`, or a begun write's `BACKUP_TTL`, the wait goes on, for the server to end it. */
const ARMED_MARGIN_MS = 60_000

/**
 * Queue the job, then wait as activity_watch does until it ends, the call is aborted or the
 * job's own time is up. The wait follows the job's current `expires_at` as each read answers it
 * (arming moves it), to one poll past it (#7273 I1), so the answer is the job's real state, never
 * a guess; an armed job gets `ARMED_MARGIN_MS` past it. A write that has begun (`write_begun`: its
 * tag is backed up, #7419 review) is never cancelled: the server ends it with its result, or
 * `failed` `no_read_back` after `BACKUP_TTL`, and the wait runs to that, from when it was first
 * seen begun, plus the margin. Any other job still open then, or when the call is aborted, is
 * cancelled first, so no page can arm it later (M1).
 */
async function followJob(
  ctx: ToolContext,
  body: { kind: 'read' | 'write'; ttl_s: number; records?: z.infer<typeof RECORD>[] },
): Promise<JobOutcome> {
  const made: Job = await ok(ctx.backend.POST('/api/v1/filament/nfc-jobs', { body }), `queue an NFC ${body.kind}`)
  const job_id = made.id
  const path = { params: { path: { job_id } } }
  const read = async () => ok(ctx.backend.GET('/api/v1/filament/nfc-jobs/{job_id}', path), `read NFC job ${job_id}`)
  let begunSeen: number | undefined
  const deadline = (job: Job) => {
    if (job.write_begun) return (begunSeen ??= Date.now()) + BACKUP_TTL_MS + ARMED_MARGIN_MS
    return Date.parse(job.expires_at) + (job.state === 'armed' ? ARMED_MARGIN_MS : ctx.pollIntervalMs)
  }
  let found: Job = made
  const wait = async () => {
    while (!ENDED.has(found.state) && Date.now() < deadline(found) && !ctx.signal.aborted) {
      try {
        await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
      } catch (err) {
        if (ctx.signal.aborted) break
        throw err
      }
      found = await read()
      await ctx.progress(0, undefined, found.state)
    }
  }
  const begun = (): JobOutcome => ({ job_id, state: found.state, result: found.result ?? null, write_begun: true })
  await wait()
  // A begun write is never cancelled: the tag may hold the new records, and the server ends it.
  if (!ENDED.has(found.state) && found.write_begun) return begun()
  if (!ENDED.has(found.state)) {
    // The answer comes even when the cancel or the read after it fails: the last state read,
    // and why the cancel did not go through, so the agent knows the job may still be open.
    try {
      const cancel = await ctx.backend.POST('/api/v1/filament/nfc-jobs/{job_id}/cancel', path)
      const code = (cancel.error as { code?: unknown } | undefined)?.code
      if (cancel.response.status === 409 && code === 'write_begun') {
        // The write began after the last read (#7419 round 3): wait for it as for any begun write.
        found = { ...(await read()), write_begun: true }
        await wait()
        return ENDED.has(found.state) ? { job_id, state: found.state, result: found.result ?? null } : begun()
      }
      await ok(Promise.resolve(cancel), `cancel NFC job ${job_id}`)
      found = await read()
    } catch (err) {
      const cancel_failed = err instanceof Error ? err.message : String(err)
      return { job_id, state: found.state, result: found.result ?? null, cancel_failed }
    }
  }
  return { job_id, state: found.state, result: found.result ?? null }
}

type JobOutcome = { job_id: string; state: string; result: Record<string, unknown> | null; write_begun?: true; cancel_failed?: string }

const runJob = async (...args: Parameters<typeof followJob>) => json(await followJob(...args))

/** A read job's result, as filament/tags.py `finish_job` writes it. */
type ReadResult = {
  uid?: string | null
  chip?: string | null
  max_ndef_bytes?: number | null
  writable?: boolean | null
  tag?: { id?: string } | null
  error?: string
}

const PROBE_UNTOLD =
  "The page read the tag but no chip: Chrome's Web NFC tells none. Run the probe again with /scan open in the ScadBuddy app on Android."

/**
 * #8366: what a probe answers: the chip, the largest NDEF message and whether the tag takes a
 * write, as the page read them, and the tag ScadBuddy keeps them on. A page that read none of
 * them (Chrome's Web NFC, an older app) answers nulls and says so.
 */
function probeOf(outcome: JobOutcome) {
  const read = (outcome.result ?? {}) as ReadResult
  const { job_id, state, cancel_failed } = outcome
  const told = read.chip != null || read.max_ndef_bytes != null || read.writable != null
  return {
    job_id,
    state,
    ...(cancel_failed === undefined ? {} : { cancel_failed }),
    ...(state === 'done'
      ? {
          uid: read.uid ?? null,
          tag_id: read.tag?.id ?? null,
          chip: read.chip ?? null,
          max_ndef_bytes: read.max_ndef_bytes ?? null,
          writable: read.writable ?? null,
          ...(told ? {} : { note: PROBE_UNTOLD }),
        }
      : { error: read.error ?? null }),
  }
}

/** Whether the call has no person behind it (see nfc_write). */
async function withoutPerson(ctx: ToolContext): Promise<boolean> {
  if (ctx.flowTurn) return true
  if (ctx.session === undefined) return false
  return !ctx.sessions || (await withoutBackground(ctx, ctx.sessions))
}

export const nfcTools = [
  defineTool({
    name: 'nfc_read',
    label: 'Read an NFC tag',
    description:
      `Read any NFC tag through the user's phone. ${WAIT} ${HOW} Any tag, not only a ` +
      "spool's. The result is the tag's UID, its records and what ScadBuddy makes of them (the spool that holds " +
      "it, or what to do next), and, when the page is the ScadBuddy app, the tag's `chip` (NTAG213, NTAG215 or " +
      'NTAG216), `max_ndef_bytes` (the largest NDEF message it takes) and `writable` (null when the page read none).',
    input: z.object({ timeout_s: TIMEOUT }),
    // #7273 R8: it makes a job row (and its result records the tag as seen); the tap is the person's own act.
    risk: 'write',
    source: SOURCE,
    routes: ROUTES,
    waitsMs: ({ timeout_s }) => timeout_s * 1000 + ARM_MS + ARMED_MARGIN_MS,
    handler: async ({ timeout_s }, ctx) => runJob(ctx, { kind: 'read', ttl_s: timeout_s }),
  }),
  defineTool({
    name: 'nfc_probe',
    label: 'Probe an NFC tag’s chip',
    description:
      "Ask the user's phone for a tag's chip and capacity, when filament_tag_get or filament_tag_list show " +
      "`chip`, `max_ndef_bytes` or `writable` as null (a tag read only by Chrome's Web NFC, or by an older app). " +
      `It runs an NFC read job, as nfc_read does, and writes nothing to the tag. ${WAIT} The user opens ScadBuddy's ` +
      '/scan page (Scan in the nav) in the ScadBuddy app on Android; it shows the job, the user taps Arm and holds the tag. Only the ScadBuddy ' +
      'app on Android reads the chip (NTAG GET_VERSION: NTAG213, NTAG215 or NTAG216), `max_ndef_bytes` (the largest ' +
      'NDEF message the tag takes) and `writable`; ScadBuddy keeps them on the tag (`tag_id`). A Bambu tag (MIFARE ' +
      'Classic) has no NTAG chip, so it answers nulls.',
    input: z.object({ timeout_s: TIMEOUT }),
    // As nfc_read: a job row, and its result records the tag as seen; the tap is the person's own act.
    risk: 'write',
    source: SOURCE,
    routes: ROUTES,
    waitsMs: ({ timeout_s }) => timeout_s * 1000 + ARM_MS + ARMED_MARGIN_MS,
    handler: async ({ timeout_s }, ctx) => json(probeOf(await followJob(ctx, { kind: 'read', ttl_s: timeout_s }))),
  }),
  defineTool({
    name: 'nfc_write',
    label: 'Write an NFC tag',
    description:
      `Write NDEF records to an NFC tag through the user's phone. ${WAIT} Once the tag is backed up the write ` +
      'has begun, and it is waited for until its read-back, or up to 10 minutes, after which it fails with ' +
      "result.code no_read_back (the tag may hold the new records; its backup can restore it). It is never " +
      `cancelled then. ${HOW} The ` +
      'job is done only when the tag reads back as the records sent; else it fails, with what the tag holds. A flow ' +
      'run, or a session a flow run started or sent the turn of, cannot call it: the only approval is the person ' +
      'arming it on the page.',
    input: z.object({ records: z.array(RECORD).min(1).max(16), timeout_s: TIMEOUT }),
    risk: 'outward',
    // docs/superpowers/specs/2026-11-23-web-nfc-tags-design.md, Constraints: "The person's
    // arm-and-tap on the page is its approval, so no pending action is needed" (#7273 R8).
    approval: 'none',
    source: SOURCE,
    routes: ROUTES,
    waitsMs: ({ timeout_s }) => timeout_s * 1000 + ARM_MS + BACKUP_TTL_MS + 2 * ARMED_MARGIN_MS,
    handler: async ({ records, timeout_s }, ctx) => {
      // Its one approval is a person's Arm on the scan page (`approval: 'none'`), so a call with no
      // person behind it never queues one: a flow run's call, a turn a flow run sent, a session a flow
      // run started or forked, or a Workflow agent's chat (`withoutBackground`, as sessions_* reads
      // them). A session call it cannot check is refused too.
      if (await withoutPerson(ctx)) throw new ToolError('nfc_write cannot run from a flow run or a session no person drives: a person must ask for it')
      return runJob(ctx, { kind: 'write', records, ttl_s: timeout_s })
    },
  }),
]
