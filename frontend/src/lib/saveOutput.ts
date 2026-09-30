import { api, ApiError } from '../api/client'
import type { Job, Output } from '../api/types'
import { joinInputs, type InputsExtra, type JsonObject } from './inputs'

/** How often a render started for Generate is read until it ends. */
const POLL_MS = 500

/**
 * What the API answers when a template's own pipeline job is saved with inputs it did
 * not render (a UI-state-only change starts no render, spec 2026-09-27 §3.4, §8.4).
 */
const NOT_RENDERED = /^inputs are not the ones job /

/**
 * Generate (spec §6): keep the render as an output, with the inputs on screen and the
 * preview as its thumbnail. A pipeline job with several outputs saves each (§5.2); the
 * first is the one returned.
 */
export async function saveOutput({
  slug,
  job,
  extra,
  capture,
}: {
  slug: string
  job: Job
  extra: InputsExtra
  capture: () => Promise<Blob | null>
}): Promise<Output> {
  const inputs = joinInputs(job.params ?? {}, extra)
  let saved = job
  let created: Output
  try {
    created = await api.createOutput(slug, job.id, undefined, inputs)
  } catch (cause) {
    if (!(cause instanceof ApiError && cause.status === 422 && NOT_RENDERED.test(cause.detail))) throw cause
    // A template's pipeline reads the whole inputs: render these ones, then save that job.
    saved = await renderFor(slug, job, inputs)
    created = await api.createOutput(slug, saved.id, undefined, inputs)
  }
  for (let index = 1; index < (saved.outputs ?? []).length; index++) {
    await api.createOutput(slug, saved.id, undefined, inputs, index)
  }
  const png = await capture()
  // A missing thumbnail is cosmetic: never fail the generate over it.
  if (png) await api.putThumbnail(created.id, png).catch(() => undefined)
  return created
}

/** A render of ``inputs`` at the revision ``job`` rendered, once it has finished. */
async function renderFor(slug: string, job: Job, inputs: JsonObject): Promise<Job> {
  const { job_id } = await api.render(slug, inputs, job.model_version ?? undefined)
  for (;;) {
    const next = await api.getJob(job_id)
    if (next.status === 'done') return next
    if (next.status === 'failed' || next.status === 'cancelled') {
      throw new ApiError(422, next.error ?? `The render for these inputs ${next.status}.`)
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS))
  }
}
