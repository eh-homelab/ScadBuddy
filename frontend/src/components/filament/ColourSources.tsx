import { RefreshCwIcon } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../../api/client'
import { listColourSyncs, startColourSync, type ColourSync, type ColourSyncSource } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'

/** How often the panel reads a running sync's progress. */
const POLL_MS = 1500

const SOURCES: { source: ColourSyncSource; title: string; action: string; about: string }[] = [
  {
    source: 'filamentcolors',
    title: 'filamentcolors.xyz',
    action: 'Sync from filamentcolors',
    about: 'Measured swatches of many makers. Reads only what changed since the last sync.',
  },
  {
    source: 'bambu',
    title: "Bambu Studio's colour codes",
    action: 'Add Bambu Lab colours',
    about: 'Bambu Lab’s named colours, exact RGBA (translucent ones keep alpha 80).',
  },
]

const count = (n: number) => n.toLocaleString()

/** What a run did, in one sentence. */
function outcome(run: ColourSync): string {
  if (run.state === 'running') {
    const page = run.pages_total ? `Reading page ${count(Math.min(run.pages_read + 1, run.pages_total))} of ${count(run.pages_total)}` : 'Starting'
    return `${page}… ${count(run.added)} added so far.`
  }
  if (run.state === 'unchanged') return 'No new swatches since the last sync, so nothing was read.'
  const parts = [`${count(run.added)} added`, `${count(run.updated)} changed`, `${count(run.unchanged)} already up to date`]
  if (run.kept) parts.push(`${count(run.kept)} kept (edited by a person, or from another source)`)
  if (run.skipped) parts.push(`${count(run.skipped)} skipped`)
  const what = parts.join(', ')
  if (run.state === 'failed') return `Stopped: ${run.error ?? 'the source could not be read'}. ${what}; what was read before stays.`
  return `Done: ${what}.`
}

/**
 * #6907 — the Colours catalog's sources: filamentcolors.xyz and Bambu Studio's colour codes
 * (ScadBuddy's own syncs, `POST /filament/colour-syncs`), and a word on Bambuddy's colours,
 * which the Bambuddy pull fills. A run's progress is read while it runs; `onSynced` reloads
 * the table once it ends.
 */
export function ColourSources({ onSynced }: { onSynced: () => void }) {
  const [runs, setRuns] = useState<ColourSync[]>([])
  const [busy, setBusy] = useState<ColourSyncSource | null>(null)
  const [error, setError] = useState<string | null>(null)

  const read = useCallback(async () => {
    try {
      setRuns(await listColourSyncs())
    } catch {
      // The table stays usable; the next poll or press reads again.
    }
  }, [])

  useEffect(() => {
    void read()
  }, [read])

  // Polls while any run it read is running, whoever started it (another tab, the agent),
  // and while its own press waits; a run past `RUN_STALE` the server reads as failed.
  const polling = busy !== null || runs.some((one) => one.state === 'running')
  useEffect(() => {
    if (!polling) return
    const timer = setInterval(() => void read(), POLL_MS)
    return () => clearInterval(timer)
  }, [polling, read])

  // A run that ended while polled (one this panel did not start too) reloads the table.
  const synced = useRef(onSynced)
  useEffect(() => {
    synced.current = onSynced
  })
  const wasRunning = useRef(new Set<ColourSyncSource>())
  useEffect(() => {
    const now = new Set(runs.filter((one) => one.state === 'running').map((one) => one.source))
    const ended = [...wasRunning.current].some((source) => !now.has(source))
    wasRunning.current = now
    if (ended) synced.current()
  }, [runs])

  async function sync(source: ColourSyncSource, force = false) {
    setBusy(source)
    setError(null)
    try {
      const run = await startColourSync(source, force)
      setRuns((all) => [...all.filter((one) => one.source !== source), run])
      onSynced()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The sync could not start. Try again.')
    } finally {
      setBusy(null)
      void read()
    }
  }

  return (
    <section aria-label="Colour sources" className="flex flex-col gap-2 rounded-md border p-3 text-[13px]">
      <p className="text-muted-foreground">
        Bambuddy’s colour catalog comes in with each Bambuddy pull. A colour a person added or edited is never overwritten, and no sync deletes a colour.
      </p>
      {SOURCES.map(({ source, title, action, about }) => {
        const run = runs.find((one) => one.source === source)
        const running = busy === source || run?.state === 'running'
        const fraction = run?.state === 'running' && run.pages_total ? Math.min(1, run.pages_read / run.pages_total) : null
        return (
          <div key={source} className="flex flex-col gap-1 border-t pt-2 first-of-type:border-t-0 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
            <div className="flex min-w-0 flex-col gap-0.5">
              <span className="font-medium">{title}</span>
              <span className="text-muted-foreground">{about}</span>
              {run && (
                <span role="status" className={run.state === 'failed' ? 'text-warn' : 'text-muted-foreground'}>
                  {outcome(run)}
                </span>
              )}
              {fraction !== null && <progress aria-label={`${title} progress`} value={fraction} max={1} className="h-1.5 w-full max-w-xs accent-primary" />}
            </div>
            <div className="flex shrink-0 gap-2">
              {run?.state === 'unchanged' && source === 'filamentcolors' && (
                <Button type="button" size="sm" variant="ghost" disabled={busy !== null} onClick={() => void sync(source, true)}>
                  Read every page
                </Button>
              )}
              <Button type="button" size="sm" variant="outline" disabled={busy !== null || running} onClick={() => void sync(source)}>
                {running ? <Spinner /> : <RefreshCwIcon />} {action}
              </Button>
            </div>
          </div>
        )
      })}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
