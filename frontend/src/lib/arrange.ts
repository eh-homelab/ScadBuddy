import { api, ApiError, NEEDS_BACKFILL } from '../api/client'
import type { ArrangeRequest, NeedsBackfillProblem, Output } from '../api/types'

export type ArrangeGoal = NonNullable<ArrangeRequest['goal']>

export const GOAL_LABELS: Record<ArrangeGoal, string> = {
  fewest_plates: 'Fewest plates',
  fewest_swaps: 'Fewest filament swaps',
  by_colour: 'One colour per plate (no prime tower)',
  keep_together: 'Keep groups together',
}

/** A saved arrange, and how many plates its file has. */
export interface Arranged {
  output: Output
  plates: number
  /** #902 — which outputs could not be re-rendered first, and why: left out of the arrange. */
  skipped?: string
}

const ARRANGED = ' (arranged)'

/** A re-arranged output's name: one "(arranged)", however often it is arranged again. */
export function arrangedName(name: string | null | undefined): string | null {
  if (!name) return null
  return name.endsWith(ARRANGED) ? name : `${name}${ARRANGED}`
}

export function arrangedNote(plates: number): string {
  return `Arranged onto ${plates} ${plates === 1 ? 'plate' : 'plates'}.`
}

/**
 * spec 2026-09-27 §7 — lay objects out again, wait for the job, save its output. The
 * plate count is the job's: `plates` lists every plate of the new file and is empty
 * when there is only one. A failed or cancelled job rejects with the job's own error.
 * An aborted `signal` stops the wait and rejects with its reason: nothing is saved
 * after the caller has gone (a closed dialog), so a job is saved once.
 */
export async function runArrange(
  slug: string,
  body: ArrangeRequest,
  opts: { pollMs?: number; onProgress?: (message: string) => void; signal?: AbortSignal } = {},
): Promise<Arranged> {
  const { signal } = opts
  const started = await api.arrangeOutputs(body)
  const pollMs = opts.pollMs ?? 1000
  for (;;) {
    signal?.throwIfAborted()
    const job = await api.getJob(started.id)
    signal?.throwIfAborted()
    if (job.status === 'done') {
      const output = await api.createOutput(slug, job.id, body.name ?? undefined)
      return { output, plates: Math.max(1, (job.plates ?? []).length) }
    }
    if (job.status === 'failed' || job.status === 'cancelled') {
      throw new Error(job.error ?? `The arrange was ${job.status}.`)
    }
    opts.onProgress?.(job.status === 'running' ? 'Arranging…' : 'Waiting for a worker…')
    await pause(pollMs, signal)
  }
}

/** `ms`, or less when `signal` aborts; the caller checks the signal after. */
function pause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve) => {
    const timer = setTimeout(done, ms)
    function done() {
      clearTimeout(timer)
      signal?.removeEventListener('abort', done)
      resolve()
    }
    signal?.addEventListener('abort', done)
  })
}

/** An output as a caller may know it: its id, and its name when it has one. */
export type OutputRef = Pick<Output, 'id'> & Partial<Pick<Output, 'name'>>

/** Has no recorded objects, so Arrange refuses it until it is re-rendered (#902). */
export function needsBackfill(output: Partial<Pick<Output, 'manifest'>>): boolean {
  return (output.manifest ?? []).length === 0
}

/** The outputs Arrange's `needs_backfill` refusal names, or null for any other failure. */
export function backfillIds(cause: unknown): string[] | null {
  if (!(cause instanceof ApiError)) return null
  const problem = cause.problem as Partial<NeedsBackfillProblem>
  if (problem.code !== NEEDS_BACKFILL) return null
  const ids = Array.isArray(problem.output_ids) ? problem.output_ids.map(String) : []
  // A refusal naming no output has nothing to offer a re-render of: show its detail.
  return ids.length > 0 ? ids : null
}

/** "A", "A and B", "A, B and C". */
export function listNames(outputs: OutputRef[]): string {
  const names = outputs.map((o) => o.name ?? o.id)
  if (names.length <= 1) return names[0] ?? ''
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/** Why Arrange cannot use them yet, in one sentence. */
export function backfillNote(outputs: OutputRef[]): string {
  const one = outputs.length === 1
  return `${listNames(outputs)} ${one ? 'was' : 'were'} saved before Arrange existed; re-render to get ${one ? 'its' : 'their'} layout.`
}

export interface Backfilled {
  /** Re-rendered, read back with their objects. */
  ready: Output[]
  /** Not re-rendered, each with why. */
  /** Not re-rendered, each with why; `running` when it was still going at the wait's limit. */
  failed: { output: OutputRef; error: string; running?: boolean }[]
}

/** "A could not be re-rendered: why." for each failure, one sentence each. */
export function backfillFailures(failed: Backfilled['failed']): string {
  return failed
    .map(({ output, error, running }) =>
      running
        ? `${output.name ?? output.id} is still re-rendering; try Arrange again later.`
        : `${output.name ?? output.id} could not be re-rendered: ${error.replace(/\.$/, '')}.`,
    )
    .join(' ')
}

const BACKFILL_POLL_MS = 500
/** How long the dialog waits for one re-render; the server keeps going after it gives up. */
export const BACKFILL_WAIT_MS = 5 * 60_000

/** The wait's limit passed with the re-render still going. */
class StillRunning extends Error {}

/**
 * #902 — re-render each output saved before Arrange, all at once: queue it, read its
 * job until it ends, then read the output until the server has attached what the
 * render recorded (`backfill` gone) or says why not (`backfill.error`). An aborted
 * `signal` stops every wait and rejects; the server still finishes the re-renders.
 */
export async function backfillOutputs(
  outputs: OutputRef[],
  opts: {
    pollMs?: number
    waitMs?: number
    onProgress?: (output: OutputRef, message: string) => void
    signal?: AbortSignal
  } = {},
): Promise<Backfilled> {
  const { signal } = opts
  const pollMs = opts.pollMs ?? BACKFILL_POLL_MS
  const deadline = Date.now() + (opts.waitMs ?? BACKFILL_WAIT_MS)
  const wait = async () => {
    if (Date.now() >= deadline) throw new StillRunning('still re-rendering')
    await pause(pollMs, signal)
  }
  const one = async (output: OutputRef): Promise<Output> => {
    const say = (message: string) => opts.onProgress?.(output, message)
    say('Queuing a re-render…')
    let job
    try {
      job = await api.backfillOutput(output.id)
    } catch (cause) {
      // Re-rendered since the list was read (a closed dialog's backfill finished).
      if (cause instanceof ApiError && cause.status === 409) return await api.getOutput(output.id)
      throw cause
    }
    for (;;) {
      signal?.throwIfAborted()
      if (job.status === 'failed' || job.status === 'cancelled') {
        throw new Error(job.error ?? `the re-render was ${job.status}`)
      }
      if (job.status === 'done') break
      say(job.status === 'running' ? 'Re-rendering…' : 'Waiting for a worker…')
      await wait()
      signal?.throwIfAborted()
      job = await api.getJob(job.id)
    }
    say('Recording its layout…')
    for (;;) {
      signal?.throwIfAborted()
      const read = await api.getOutput(output.id)
      if (read.backfill?.error) throw new Error(read.backfill.error)
      if (!read.backfill) {
        if (needsBackfill(read)) throw new Error('the re-render recorded no objects')
        return read
      }
      await wait()
    }
  }
  const settled = await Promise.allSettled(outputs.map(one))
  signal?.throwIfAborted()
  const result: Backfilled = { ready: [], failed: [] }
  settled.forEach((outcome, index) => {
    const output = outputs[index]!
    if (outcome.status === 'fulfilled') result.ready.push(outcome.value)
    else {
      const cause = outcome.reason as unknown
      const error = cause instanceof ApiError ? cause.detail : (cause as Error).message
      result.failed.push(cause instanceof StillRunning ? { output, error, running: true } : { output, error })
    }
  })
  return result
}
