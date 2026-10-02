import { api } from '../api/client'
import type { Job, Output } from '../api/types'
import { joinInputs, type InputsExtra } from './inputs'

/** Generate (spec §6): keep the render as an output, with the inputs on screen and the preview as its thumbnail. */
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
  const created = await api.createOutput(slug, job.id, undefined, joinInputs(job.params ?? {}, extra))
  const png = await capture()
  // A missing thumbnail is cosmetic: never fail the generate over it.
  if (png) await api.putThumbnail(created.id, png).catch(() => undefined)
  return created
}
