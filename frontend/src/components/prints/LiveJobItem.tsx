import { Link } from 'react-router'
import type { QueueItem } from '../../api/types'
import { formatDuration } from '../../lib/format'
import type { PrintsView } from '../../lib/printsQuery'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { JobPicture } from './JobPicture'
import { liveLabel, type LiveJob } from './liveJobs'
import { JobAnchor, JobProgress } from './NowPrinting'
import { lastSeen } from './nowCards'
import { queueItemPath, scadbuddyJobPath } from './prints'
import { QueuedFromScadBuddy } from './QueuedFromScadBuddy'

const TONE: Record<string, string> = {
  queued: 'text-muted-foreground',
  printing: 'border-primary/30 bg-muted',
  paused: 'border-warn/40 bg-warn/10 text-warn',
}

/** Its state's tone, beside the words that carry it. */
function tone(job: LiveJob): string {
  if (job.kind === 'queued') return TONE.queued!
  return job.card.printer.activity === 'paused' ? TONE.paused! : TONE.printing!
}

/**
 * #3745 — a live job's state as a badge: always words ("Queued · position 3", "Printing
 * 66% · 54m left", "Paused: …"), the colour only beside them. A printing job has the
 * history's pulsing dot.
 */
export function LiveStatus({ job }: { job: LiveJob }) {
  const printing = job.kind === 'working' && job.card.printer.activity === 'printing'
  return (
    <Badge
      variant="outline"
      data-status={job.kind === 'queued' ? 'queued' : job.card.printer.activity}
      className={cn('h-auto max-w-full shrink font-normal whitespace-normal', tone(job))}
    >
      {printing && (
        <span aria-hidden="true" className="size-1.5 animate-pulse rounded-full bg-primary motion-reduce:animate-none" />
      )}
      {liveLabel(job)}
    </Badge>
  )
}

/**
 * Under a live row's name: the "Queued from ScadBuddy" mark (#3741), and a working job's
 * progress, or a queued item's printer, estimate and why it waits. #5940 — a working
 * job whose printer's card is stale says so, in the card's words: its state is the
 * last one read.
 */
export function LiveDetails({ job }: { job: LiveJob }) {
  const scadbuddy = job.kind === 'queued' ? job.item.scadbuddy : job.card.printer.scadbuddy
  const stale = job.kind === 'working' && job.card.stale ? job.card : null
  return (
    <>
      {scadbuddy && <QueuedFromScadBuddy job={scadbuddy} className="mt-1" />}
      {stale && (
        <p
          className="mt-1 text-xs text-muted-foreground"
          data-testid={`printer-job-stale-${stale.printer.printer_id ?? stale.printer.id}`}
        >
          {lastSeen(stale)}
        </p>
      )}
      {job.kind === 'working' ? <JobProgress printer={job.card.printer} /> : <QueueFacts item={job.item} />}
    </>
  )
}

function QueueFacts({ item }: { item: QueueItem }) {
  const where = item.printer_name ?? (item.target_model ? `Any ${item.target_model}` : 'No printer yet')
  const notes: string[] = []
  if (item.scheduled_time) notes.push(`Scheduled ${new Date(item.scheduled_time).toLocaleString()}`)
  if (item.manual_start) notes.push('Waits for a manual start')
  if (item.waiting_reason) notes.push(item.waiting_reason)
  return (
    <>
      <p className="sb-num mt-1 text-xs text-muted-foreground">
        {where}
        {item.print_time_seconds ? ` · ${formatDuration(item.print_time_seconds)}` : ''}
        {item.filament_used_grams ? ` · ${Math.round(item.filament_used_grams)} g` : ''}
      </p>
      {item.filament_short && <p className="mt-0.5 text-xs text-warn">Not enough filament loaded for this print.</p>}
      {notes.length > 0 && <p className="mt-0.5 text-xs text-muted-foreground">{notes.join(' · ')}</p>}
    </>
  )
}

/** The name's link stretches over the whole row, as a history row's does (#311). */
const STRETCHED =
  "outline-none after:absolute after:inset-0 after:rounded-xl after:content-[''] focus-visible:after:ring-3 focus-visible:after:ring-ring/50"

/**
 * #3743, #3744 — a queued item's name, as a link in ScadBuddy: ScadBuddy's own item to
 * its ScadBuddy page ({@link scadbuddyJobPath}: its print once linked, else its queue
 * item page); any other to its queue item page, which links on to Bambuddy's queue.
 * Bambuddy has no route for one item (`frontend/src/App.tsx` and `QueuePage.tsx` at
 * maziggy/bambuddy@2ce0d93).
 */
function QueuedName({ item }: { item: QueueItem }) {
  const to = item.scadbuddy ? scadbuddyJobPath(item.scadbuddy, item.id) : queueItemPath(item.id)
  return (
    <Link to={to} className={STRETCHED}>
      {item.archive_name ?? item.library_file_name ?? `Queue item ${item.id}`}
    </Link>
  )
}

/**
 * #3745 — a print with no archived row in the list yet, in the history's card: a
 * queued item, or a printer's job the history does not list (one Bambuddy queued from
 * elsewhere, or one not linked yet). Its picture is #3742's, its mark #3741's and its
 * link #3743's; its state is {@link LiveStatus}.
 */
export function LiveJobItem({ job, view }: { job: LiveJob; view: PrintsView }) {
  const cards = view === 'cards'
  const picture = job.kind === 'queued' ? job.item.picture : job.card.printer.picture
  const testId =
    job.kind === 'queued'
      ? `queue-item-${job.item.id}`
      : `printer-job-${job.card.printer.printer_id ?? job.card.printer.id}`
  return (
    <li data-job={job.key} data-testid={testId} className="min-w-0">
      <Card
        className={cn(
          'relative h-full gap-0 py-0 transition-shadow hover:ring-foreground/20 hover:shadow-sm',
          cards ? 'flex-col' : 'flex-row items-start gap-3 p-2.5',
        )}
      >
        <div className={cards ? 'p-3 pb-0' : 'w-24 shrink-0 sm:w-28'}>
          <JobPicture src={picture} className="aspect-[4/3] size-auto w-full rounded-lg border-0" />
        </div>
        <div className={`min-w-0 flex-1 ${cards ? 'px-3 pt-2.5 pb-3' : ''}`}>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <h2 className="min-w-0 truncate text-sm font-medium">
              {job.kind === 'queued' ? (
                <QueuedName item={job.item} />
              ) : (
                <JobAnchor printer={job.card.printer} className={STRETCHED} />
              )}
            </h2>
            <LiveStatus job={job} />
          </div>
          <LiveDetails job={job} />
        </div>
      </Card>
    </li>
  )
}
