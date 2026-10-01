import { api } from '../api/client'
import type { ArrangeRequest, Output } from '../api/types'

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
 */
export async function runArrange(
  slug: string,
  body: ArrangeRequest,
  opts: { pollMs?: number; onProgress?: (message: string) => void } = {},
): Promise<Arranged> {
  const started = await api.arrangeOutputs(body)
  const pollMs = opts.pollMs ?? 1000
  for (;;) {
    const job = await api.getJob(started.id)
    if (job.status === 'done') {
      const output = await api.createOutput(slug, job.id, body.name ?? undefined)
      return { output, plates: Math.max(1, (job.plates ?? []).length) }
    }
    if (job.status === 'failed' || job.status === 'cancelled') {
      throw new Error(job.error ?? `The arrange was ${job.status}.`)
    }
    opts.onProgress?.(job.status === 'running' ? 'Arranging…' : 'Waiting for a worker…')
    await new Promise((resolve) => setTimeout(resolve, pollMs))
  }
}
