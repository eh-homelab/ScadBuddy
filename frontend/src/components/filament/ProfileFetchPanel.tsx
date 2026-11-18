import { CheckIcon, CloudDownloadIcon, LoaderCircleIcon, TriangleAlertIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../../api/client'
import { getProfileFetch, startProfileFetch, type MissingKey, type ProfileFetch, type ProfileFetchStatus } from '../../api/filament'
import { AppLink } from '../AppLink'
import { Button } from '@/components/ui/button'

/** How often a running fetch is read again (the brief: every 10 s). */
export const PROFILE_FETCH_POLL_MS = 10_000

const STATUS_WORD: Record<ProfileFetch['status'], string> = {
  running: 'Running',
  imported: 'Imported',
  partial: 'Partly imported',
  failed: 'Failed',
}

const message = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

/** A refused start, in plain words: the API's two codes, else its own detail. */
function refusal(cause: unknown): { text: string; printers?: boolean } {
  if (cause instanceof ApiError && cause.problem.code === 'no_tdfp_id') return { text: 'This type has no saved 3DFP id. Enter it above and save the type.' }
  if (cause instanceof ApiError && cause.problem.code === 'no_printer_models') return { text: 'No printer has a model, so there is nothing to fetch presets for.', printers: true }
  return { text: message(cause, 'The server could not be reached. Try again.') }
}

/** "H2C with a 0.4 mm nozzle". */
const keyWords = (key: Pick<MissingKey, 'printer_model' | 'nozzle_diameter'>) => `${key.printer_model} with a ${key.nozzle_diameter} mm nozzle`

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** What the run is fetching or did, in one line. */
function summary(run: ProfileFetch): string {
  const keys = run.targets.reduce((n, target) => n + target.nozzles.length, 0)
  const models = run.targets.map((target) => target.printer_model).join(', ')
  if (run.status === 'running') return `Exporting ${plural(keys, 'preset', 'presets')} for ${models} from 3DFP.`
  const when = run.finished_at ? new Date(run.finished_at).toLocaleString() : null
  const k = run.k_values > 0 ? ` ${plural(run.k_values, 'K value was', 'K values were')} read from the printers.` : ''
  if (run.status === 'failed') return `${when ? `Ended ${when}. ` : ''}No preset was set.`
  const set = keys - run.missing.length
  return `${when ? `Ended ${when}. ` : ''}${plural(set, 'preset was', 'presets were')} set for ${models}.${k}`
}

/**
 * #6625 — "Fetch from 3DFP" on a spool type's presets: starts (or joins) a fetch of the
 * type's slicer presets from 3dfilamentprofiles.com by its saved `tdfp_id`, and shows the
 * type's last fetch: its status, the keys it could not set and why (`missing`), its notes
 * and its error. While the run is `running` it is read again every `pollMs`; a failed read
 * does not stop the reads, and the next good one clears its error. `onEnded` is called once
 * when the panel saw a run running and then reads an ended one, so the page can read the
 * presets it set. The button is off, and says why, while the type is not saved (`typeId`
 * null), its 3DFP id has an unsaved edit (`idEdited`) or it has no saved 3DFP id (`tdfpId`),
 * and while a fetch runs.
 */
export function ProfileFetchPanel({
  typeId,
  tdfpId,
  idEdited,
  onEnded,
  pollMs = PROFILE_FETCH_POLL_MS,
}: {
  typeId: string | null
  tdfpId: number | null
  idEdited: boolean
  onEnded: (run: ProfileFetch) => void
  pollMs?: number
}) {
  const [status, setStatus] = useState<ProfileFetchStatus | null>(null)
  const [readError, setReadError] = useState<string | null>(null)
  const [startError, setStartError] = useState<{ text: string; printers?: boolean } | null>(null)
  const [joined, setJoined] = useState(false)
  const [starting, setStarting] = useState(false)
  const [reads, setReads] = useState(0)
  const [settled, setSettled] = useState(0)
  /** Whether this panel saw a run `running` whose end it has not reported yet. */
  const watching = useRef(false)
  const ended = useRef(onEnded)
  useEffect(() => {
    ended.current = onEnded
  }, [onEnded])

  useEffect(() => {
    if (typeId === null) return
    let live = true
    getProfileFetch(typeId).then(
      (read) => {
        if (!live) return
        setStatus(read)
        setReadError(null)
        setSettled((n) => n + 1)
      },
      (cause: unknown) => {
        if (!live) return
        setReadError(message(cause, 'The last fetch could not be read.'))
        setSettled((n) => n + 1)
      },
    )
    return () => {
      live = false
    }
  }, [typeId, reads])

  const run = status?.run ?? null
  const running = run?.status === 'running'

  useEffect(() => {
    if (!run) return
    if (run.status === 'running') {
      watching.current = true
    } else if (watching.current) {
      // Any run that ended since, not only the one seen running: the API may have ended
      // that one and started another between two reads.
      watching.current = false
      setJoined(false)
      ended.current(run)
    }
  }, [run])

  useEffect(() => {
    if (!running) return
    const timer = setTimeout(() => setReads((n) => n + 1), pollMs)
    return () => clearTimeout(timer)
  }, [running, settled, pollMs])

  async function start() {
    if (typeId === null) return
    setStarting(true)
    setStartError(null)
    try {
      const started = await startProfileFetch(typeId)
      setJoined(started.joined)
      setStatus((was) => ({ run: started.run, gaps: was?.gaps ?? [] }))
    } catch (cause) {
      setStartError(refusal(cause))
      // A 503 ends the run `failed`: read it, so the panel shows it.
      setReads((n) => n + 1)
    } finally {
      setStarting(false)
    }
  }

  const why =
    typeId === null
      ? 'Save the type, with its 3DFP id, to fetch its presets.'
      : idEdited
        ? 'Save the type first: the fetch uses the saved 3DFP id.'
        : tdfpId === null
          ? 'Enter the 3DFP id above and save the type to fetch its presets.'
          : null

  return (
    <div className="flex flex-col gap-2 rounded-md border p-3" aria-label="Fetch from 3DFP" role="group">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={why !== null || starting || running}
          aria-describedby={why ? 'profile-fetch-why' : undefined}
          onClick={() => void start()}
        >
          {running ? <LoaderCircleIcon className="animate-spin motion-reduce:animate-none" /> : <CloudDownloadIcon />}
          Fetch from 3DFP
        </Button>
        {why ? (
          <p id="profile-fetch-why" className="text-[12px] text-muted-foreground">
            {why}
          </p>
        ) : (
          !run && <p className="text-[12px] text-muted-foreground">Imports a preset for each printer model and nozzle into Bambuddy, and sets it here.</p>
        )}
      </div>

      {run && (
        <div role="status" aria-label="Last fetch" className="flex flex-col gap-1.5 text-[13px]">
          <p className="flex flex-wrap items-center gap-x-2">
            <StatusMark status={run.status} />
            <span className="text-muted-foreground">{summary(run)}</span>
          </p>
          {joined && running && <p className="text-[12px] text-muted-foreground">A fetch of this type was already running, so this follows it.</p>}
          {run.error && <p className="text-warn">{run.error}</p>}
          {run.missing.length > 0 && (
            <div>
              <p>{run.missing.length === 1 ? 'This preset was not set:' : 'These presets were not set:'}</p>
              <ul aria-label="Presets not set" className="ml-4 list-disc">
                {run.missing.map((key) => (
                  <li key={`${key.printer_model}|${key.nozzle_diameter}`}>
                    {keyWords(key)}: {key.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {run.notes.length > 0 && (
            <div>
              <p>Notes:</p>
              <ul aria-label="Fetch notes" className="ml-4 list-disc text-muted-foreground">
                {run.notes.map((note, at) => (
                  <li key={at}>{note}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {(startError ?? readError) && (
        <p role="alert" className="text-[13px] text-warn">
          {startError?.text ?? readError}
          {startError?.printers && (
            <>
              {' '}
              <AppLink href="/settings/printing#printers" className="underline underline-offset-2">
                Set a model in Printers
              </AppLink>
              .
            </>
          )}
        </p>
      )}
    </div>
  )
}

function StatusMark({ status }: { status: ProfileFetch['status'] }) {
  const tone = status === 'imported' ? 'text-ok' : status === 'running' ? 'text-foreground' : 'text-warn'
  const Icon = status === 'imported' ? CheckIcon : status === 'running' ? LoaderCircleIcon : TriangleAlertIcon
  return (
    <span className={`inline-flex items-center gap-1 font-medium ${tone}`}>
      <Icon aria-hidden className={`size-3.5 ${status === 'running' ? 'animate-spin motion-reduce:animate-none' : ''}`} />
      {STATUS_WORD[status]}
    </span>
  )
}
