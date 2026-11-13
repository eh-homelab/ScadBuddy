import { api, ApiError } from '../../api/client'
import type { Filament, Job, ModelSummary, ParamValue } from '../../api/types'
import { defaultsFor, emptyDraft, sellMedia, type ListingDraft } from './listingDraft'

/**
 * #6131 (design 2026-11-13 §2, §3) — "Sell on storefront": the listing the Storefront page
 * opens for `?sell=job:<id>` (the Customize page's render of the values on screen) or
 * `?sell=output:<id>` (a saved output), filled in from what the person was looking at.
 */

/** How long a render may take before the storefront's preview and price give up (#6120). */
export const SLOW_RENDER_SECONDS = 100

export interface SellListing {
  draft: ListingDraft
  /** What the person should know before publishing: a version taken instead, a slow render. */
  notes: string[]
}

/** The `sell` address parameter for a render job or a saved output. */
export function sellPath(source: { job: string } | { output: string }): string {
  const value = 'job' in source ? `job:${source.job}` : `output:${source.output}`
  return `/storefront?${new URLSearchParams({ sell: value }).toString()}`
}

function seconds(job: Pick<Job, 'started_at' | 'finished_at'> | null): number | null {
  if (!job?.started_at || !job.finished_at) return null
  return (Date.parse(job.finished_at) - Date.parse(job.started_at)) / 1000
}

interface Source {
  slug: string
  version: string | null
  name: string | null
  values: Record<string, ParamValue>
  job: Job | null
}

async function source(sell: string): Promise<Source> {
  const at = sell.indexOf(':')
  const kind = sell.slice(0, at)
  const id = sell.slice(at + 1)
  if (at > 0 && id && kind === 'job') {
    const job = await api.getJob(id)
    return { slug: job.slug, version: job.model_version ?? null, name: null, values: job.params ?? {}, job }
  }
  if (at > 0 && id && kind === 'output') {
    const output = await api.getOutput(id)
    // The render's time only: a job pruned since leaves no warning, never a refusal.
    const job = await api.getJob(output.job_id).catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 404) return null
      throw error
    })
    return { slug: output.slug, version: output.model_version ?? null, name: output.name ?? null, values: output.params ?? {}, job }
  }
  throw new Error(`Nothing to sell: ${JSON.stringify(sell)} names no render or saved output.`)
}

/** The listing `sell` names, filled in with the price book's `filaments` and the `models`' names and pictures. */
export async function sellListing(sell: string, filaments: readonly Filament[], models: readonly ModelSummary[]): Promise<SellListing> {
  const { slug, version, name, values, job } = await source(sell)
  const model = models.find((m) => m.slug === slug)
  const notes: string[] = []
  const versions = await api.listVersions(slug)
  const current = versions.find((v) => v.current) ?? versions[0]
  const pinned = versions.find((v) => version !== null && v.commit === version)
  const commit = pinned?.commit ?? current?.commit
  if (!commit) throw new Error(`${model?.name ?? slug} has no version in its history to sell.`)
  if (!pinned) notes.push(`That was made from a version that is not in the design's history, so the listing uses the current version (${current?.short ?? commit.slice(0, 7)}).`)
  const schema = await api.getSchema(slug, commit)
  const took = seconds(job)
  if (took !== null && took > SLOW_RENDER_SECONDS) {
    notes.push(`This design took ${Math.round(took)} s to render. The store's preview and price give up after about ${SLOW_RENDER_SECONDS} s, so customers may not get one.`)
  }
  return {
    draft: {
      ...emptyDraft(slug, commit),
      title: (name ?? model?.name ?? slug).slice(0, 200),
      ...defaultsFor(schema, filaments, values),
      media_ids: sellMedia(model?.media ?? []),
    },
    notes,
  }
}
