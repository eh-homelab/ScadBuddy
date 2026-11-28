import { RefreshCwIcon, SearchIcon } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { ApiError } from '../../api/client'
import { listColourSyncs, lookupBambuColourCode, startColourSync, type ColourCode, type ColourSync, type ColourSyncSource } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
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
 * #6907 — one Bambu colour code looked up in Bambu Studio's table (`GET /filament/bambu-colour-codes`):
 * the code is the digits of a Bambu spool's SKU. It shows the colour's name, each RGBA (a translucent
 * one keeps its alpha) and the filament type and id; it adds nothing to the catalog.
 */
function ColourCodeLookup() {
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [found, setFound] = useState<ColourCode | null>(null)
  const [error, setError] = useState<string | null>(null)
  const asked = useRef(0)
  // An answer that comes after the panel unmounts is dropped, as a stale one is.
  useEffect(
    () => () => {
      asked.current += 1
    },
    [],
  )

  async function lookUp(event: FormEvent) {
    event.preventDefault()
    const wanted = code.trim()
    if (!wanted) return
    const ask = ++asked.current
    setBusy(true)
    setError(null)
    setFound(null)
    try {
      const answer = await lookupBambuColourCode(wanted)
      if (ask === asked.current) setFound(answer)
    } catch (cause) {
      if (ask !== asked.current) return
      if (cause instanceof ApiError && cause.status === 404) setError(`Bambu Studio's colour table has no code ${wanted}.`)
      else setError(cause instanceof ApiError ? cause.detail : 'The code could not be looked up. Try again.')
    } finally {
      if (ask === asked.current) setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      <form onSubmit={(event) => void lookUp(event)} className="flex flex-wrap items-center gap-2">
        <Input
          aria-label="Bambu colour code"
          placeholder="The digits of a Bambu SKU"
          inputMode="numeric"
          className="h-8 w-56"
          value={code}
          onChange={(event) => setCode(event.target.value)}
        />
        <Button type="submit" size="sm" variant="ghost" disabled={busy || !code.trim()}>
          {busy ? <Spinner /> : <SearchIcon />} Look up
        </Button>
      </form>
      {found && (
        <div role="group" aria-label={`Bambu colour code ${found.code}`} className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="flex items-center gap-1">
            {found.rgba.map((rgba, at) => (
              <span
                key={at}
                role="img"
                aria-label={rgba}
                className="size-4 rounded-sm border bg-[linear-gradient(45deg,#ccc_25%,transparent_25%,transparent_75%,#ccc_75%)] bg-[length:6px_6px]"
              >
                <span className="block size-full rounded-[inherit]" style={{ backgroundColor: rgba }} />
              </span>
            ))}
          </span>
          <span className="font-medium">{found.name}</span>
          <span className="font-mono text-muted-foreground">{found.rgba.join(' ')}</span>
          <span className="text-muted-foreground">
            {found.fila_type}, filament id {found.fila_id}
          </span>
        </div>
      )}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </div>
  )
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
              {source === 'bambu' && <ColourCodeLookup />}
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
