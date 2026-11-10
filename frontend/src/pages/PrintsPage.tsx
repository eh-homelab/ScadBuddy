import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router'
import { api } from '../api/client'
import { ImportPrintDialog } from '../components/prints/ImportPrintDialog'
import { LiveJobItem } from '../components/prints/LiveJobItem'
import { liveJobs, queueTail, startedOn, type LiveJob } from '../components/prints/liveJobs'
import { PrinterStrip, usePrintersNow } from '../components/prints/NowPrinting'
import { QueueNotes, usePrintQueue } from '../components/prints/PrintQueue'
import { PrintHistory } from '../components/prints/PrintHistory'
import { modelPath } from '../lib/deeplink'
import { nativeBackends } from '../lib/printerRegistry'
import { isFiltered, parsePrintsQuery, readStoredPrintsView } from '../lib/printsQuery'
import { useAsync } from '../lib/useAsync'
import { Button } from '@/components/ui/button'

/**
 * #310 — every print of every template (`/prints`, plan §2.7), and (#3745) every print
 * with no outcome yet, in one list: each print once, its row going from queued to
 * printing to finished in place (`liveJobs`, `printRows`). Above it, the printers'
 * own state (`PrinterStrip`). The page makes the reads it made before: the printers
 * (`usePrintersNow`), Bambuddy's queue (`usePrintQueue`) and the history's pages.
 *
 * On the hot path's backends (#3818 tier A, `nativeBackends`) the queue and the archive
 * history are Bambuddy's, so they are read only once asked for (#4629): opening the
 * page asks Bambuddy nothing, and the printers and their jobs are the LAN reports'
 * (`get_printers_now`). Until Settings answer neither is read, so a native page never
 * asks first; a failed Settings read reads both, as before.
 *
 * "Import print" (#4698) links a Bambuddy archive ScadBuddy did not print to a model
 * (`ImportPrintDialog`); it reads Bambuddy, so it shows with Bambuddy's lists.
 */
export function PrintsPage() {
  const settings = useAsync(() => api.getSettings(), [])
  const settled = settings.data !== undefined || settings.error !== undefined
  const lazy = nativeBackends(settings.data)
  const [asked, setAsked] = useState(false)
  const [importing, setImporting] = useState(false)
  // #4698: an import adds a print to the history, which reads again for it.
  const [imports, setImports] = useState(0)
  const bambuddy = settled && (!lazy || asked)
  const now = usePrintersNow()
  const working = useMemo(() => (now.cards === null ? null : liveJobs(now.cards, [])), [now.cards])
  const queue = usePrintQueue(
    bambuddy,
    useMemo(
      () => working?.flatMap((job) => (job.kind === 'working' ? [startedOn(job.card.printer.id, job.key)] : [])) ?? null,
      [working],
    ),
  )
  const jobs = useMemo(() => liveJobs(now.cards ?? [], queue.items ?? []), [now.cards, queue.items])
  // A filtered list shows no queued row (`printRows`), so it says nothing of the queue.
  const [params] = useSearchParams()
  const filtered = isFiltered(parsePrintsQuery(params))
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex items-start justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Prints</h1>
            <p className="mt-0.5 text-[13px] text-subtle">
              What waits in Bambuddy's queue, what prints now, and every print of a ScadBuddy output, as Bambuddy
              archived it.
            </p>
          </div>
          {bambuddy && (
            <Button variant="outline" size="sm" onClick={() => setImporting(true)} data-testid="import-print">
              Import print
            </Button>
          )}
        </div>
        <ImportPrintDialog open={importing} onOpenChange={setImporting} onImported={() => setImports((n) => n + 1)} />
        <PrinterStrip now={now} />
        {settled &&
          (bambuddy ? (
            <>
              <QueueNotes queue={queue} tail={queueTail(queue.items ?? [])} filtered={filtered} />
              <PrintHistory key={imports} live={jobs} />
            </>
          ) : (
            <>
              <WorkingOnly jobs={jobs} />
              <Button variant="outline" onClick={() => setAsked(true)} data-testid="print-bambuddy-lists">
                Show Bambuddy's queue and print history
              </Button>
            </>
          ))}
      </div>
    </div>
  )
}

/**
 * #4629, #3745 — the list before Bambuddy's queue and history are asked for: the
 * printers' working jobs only, from the read the page already made.
 */
function WorkingOnly({ jobs }: { jobs: LiveJob[] }) {
  const [params] = useSearchParams()
  const view = params.has('view') ? parsePrintsQuery(params).view : (readStoredPrintsView() ?? 'cards')
  if (jobs.length === 0) return null
  return (
    <ul
      aria-label="Prints"
      data-view={view}
      className={
        view === 'cards' ? 'mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3' : 'mb-4 flex flex-col gap-2'
      }
    >
      {jobs.map((job) => (
        <LiveJobItem key={job.key} job={job} view={view} />
      ))}
    </ul>
  )
}

/** #310 — a template's Prints tab (`/m/:slug/prints`): the same list, for this template. */
export function TemplatePrintsPage() {
  const { slug = '' } = useParams()
  const model = useAsync(() => api.getModel(slug), [slug])
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-4 py-6">
        <div className="mb-5 flex items-baseline gap-2">
          <Link to="/" className="text-[12px] text-subtle hover:text-ink">
            Models
          </Link>
          <span className="text-faint">/</span>
          <Link to={modelPath(slug)} className="text-[12px] text-subtle hover:text-ink">
            {model.data?.name ?? slug}
          </Link>
          <span className="text-faint">/</span>
          <h1 className="text-[13px] font-medium">Prints</h1>
        </div>
        <PrintHistory key={slug} fixedSlug={slug} />
      </div>
    </div>
  )
}
