import { api, ApiError } from '../api/client'
import type { Job, Output } from '../api/types'
import { joinInputs, type InputsExtra, type JsonObject } from './inputs'
import type { Within } from './traceAction'

/** How often a render started for Generate is read until it ends. */
const POLL_MS = 500
/**
 * How long Generate waits for a render it had to start: a whole-house pipeline renders
 * every piece first, so this is minutes, not a preview's seconds.
 */
export const RENDER_WAIT_MS = 15 * 60_000

/**
 * What the API answers when a template's own pipeline job is saved with inputs it did
 * not render (a UI-state-only change starts no render, spec 2026-09-27 §3.4, §8.4).
 */
const NOT_RENDERED = /^inputs are not the ones job /

function detail(cause: unknown): string {
  return cause instanceof ApiError ? cause.detail : String(cause)
}

/**
 * The first output was saved (and reported), but the job's later ones were not.
 * `saveRemaining` takes up where it stopped, so a retry never saves the first again.
 */
export class ExtraOutputsError extends Error {
  readonly slug: string
  /** The job whose outputs were being saved: a re-render's when Generate had to render. */
  readonly job: Job
  /** The job Generate was asked to save, which a retry is matched against. */
  readonly requested: Job
  readonly inputs: JsonObject
  readonly saved: Output
  /** The first index not yet saved. */
  readonly next: number

  constructor(
    slug: string,
    job: Job,
    requested: Job,
    inputs: JsonObject,
    saved: Output,
    next: number,
    cause: unknown,
  ) {
    const total = (job.outputs ?? []).length
    const which = next === total - 1 ? `output ${total}` : `outputs ${next + 1} to ${total}`
    super(`Saved the first output; ${which} could not be saved: ${detail(cause)}`)
    this.name = 'ExtraOutputsError'
    this.slug = slug
    this.job = job
    this.requested = requested
    this.inputs = inputs
    this.saved = saved
    this.next = next
  }
}

/** Outputs ``from``..n-1 of a job with several (§5.2); the first is already saved. */
async function saveFrom(
  slug: string,
  job: Job,
  requested: Job,
  inputs: JsonObject,
  saved: Output,
  from: number,
) {
  for (let index = from; index < (job.outputs ?? []).length; index++) {
    try {
      await api.createOutput(slug, job.id, undefined, inputs, index)
    } catch (cause) {
      throw new ExtraOutputsError(slug, job, requested, inputs, saved, index, cause)
    }
  }
}

/** A retry of the outputs an `ExtraOutputsError` names, and only those. */
export function saveRemaining(failed: ExtraOutputsError): Promise<void> {
  return saveFrom(failed.slug, failed.job, failed.requested, failed.inputs, failed.saved, failed.next)
}

/**
 * Generate (spec §6): keep the render as an output, with the inputs on screen and the
 * preview as its thumbnail. A pipeline job with several outputs saves each (§5.2); the
 * first is reported through ``onSaved`` as soon as it is saved, and returned. A failure
 * saving the others is an `ExtraOutputsError`.
 */
export async function saveOutput({
  slug,
  job,
  extra,
  capture,
  onSaved,
  signal,
  renderWaitMs = RENDER_WAIT_MS,
  within = (call) => call(),
}: {
  slug: string
  job: Job
  extra: InputsExtra
  capture: () => Promise<Blob | null>
  onSaved?: (output: Output) => void
  /** Stops a render this has to wait for (the page went away). */
  signal?: AbortSignal
  renderWaitMs?: number
  /** Generate's span (`traceAction`), so the thumbnail's request, after an await, joins its trace. */
  within?: Within
}): Promise<Output> {
  const inputs = joinInputs(job.params ?? {}, extra)
  let saved = job
  let created: Output
  try {
    created = await api.createOutput(slug, job.id, undefined, inputs)
  } catch (cause) {
    if (!(cause instanceof ApiError && cause.status === 422 && NOT_RENDERED.test(cause.detail))) throw cause
    // A template's pipeline reads the whole inputs: render these ones, then save that job.
    saved = await renderFor(slug, job, inputs, signal, renderWaitMs)
    created = await within(() => api.createOutput(slug, saved.id, undefined, inputs))
  }
  const png = await capture()
  // A missing thumbnail is cosmetic: never fail the generate over it. Only the first
  // output gets one; it is the one the page shows.
  if (png) await within(() => api.putThumbnail(created.id, png)).catch(() => undefined)
  onSaved?.(created)
  await saveFrom(slug, saved, job, inputs, created, 1)
  return created
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer)
      reject(signal?.reason as Error)
    }
    // One listener per sleep, removed when it ends: a long wait does not stack them.
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/** A render of ``inputs`` at the revision ``job`` rendered, once it has finished. */
async function renderFor(
  slug: string,
  job: Job,
  inputs: JsonObject,
  signal: AbortSignal | undefined,
  waitMs: number,
): Promise<Job> {
  const deadline = Date.now() + waitMs
  signal?.throwIfAborted()
  const { job_id } = await api.render(slug, inputs, job.model_version ?? undefined)
  for (;;) {
    signal?.throwIfAborted()
    const next = await api.getJob(job_id)
    if (next.status === 'done') return next
    if (next.status === 'failed' || next.status === 'cancelled') {
      throw new ApiError(422, next.error ?? `The render for these inputs ${next.status}.`)
    }
    if (Date.now() >= deadline) {
      throw new ApiError(504, `The render for these inputs did not finish within ${Math.round(waitMs / 1000)} s.`)
    }
    await sleep(POLL_MS, signal)
  }
}
