import type { ReactNode } from 'react'
import { ChevronRightIcon, ExternalLinkIcon, SlidersHorizontalIcon, TriangleAlertIcon } from 'lucide-react'
import { Link, Navigate, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type { Output, QueueItem, ScadBuddyJob } from '../api/types'
import { JobPicture } from '../components/prints/JobPicture'
import { ParamsTable } from '../components/prints/PrintFiles'
import { PrintStatus } from '../components/prints/PrintStatus'
import { printPath } from '../components/prints/prints'
import { QueuedFromScadBuddy } from '../components/prints/QueuedFromScadBuddy'
import { statusLabel } from '../components/prints/status'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { bambuddyBase } from '../lib/bambuddyLinks'
import { editPath, modelPath } from '../lib/deeplink'
import { NEW_TAB, openExternal } from '../lib/embed'
import { amsUnitLabel } from '../lib/filaments'
import { formatDuration } from '../lib/format'
import { useAsync } from '../lib/useAsync'

/** A queue item id: what Bambuddy's ids look like, so nothing else is asked for. */
const ITEM_ID = /^[1-9]\d{0,14}$/

/** Bambuddy's answer for an item it no longer has (`api/farm.py` `get_queue_item`). */
function isGone(error: Error | null | undefined): boolean {
  return error instanceof ApiError && error.status === 404
}

function when(iso: string | null | undefined): string | null {
  if (!iso) return null
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

/**
 * The unit Bambuddy shows the A2L's AMS-Lite as: it reports unit 16 and Bambuddy folds
 * it to 6 (`backend/scadbuddy/printers/status.py` `_A2L_BITS`), which
 * {@link amsUnitLabel} names "AMS Lite".
 */
const AMS_LITE_UNIT = 6

/**
 * Whether the item prints on an A2L, the one printer with an AMS-Lite: by the model the
 * file was sliced for, else the model the item waits for. Bambuddy names it "A2L" (the
 * printer code N9, `backend/scadbuddy/bambuddy/filaments3mf.py`); a full name such as
 * "Bambu Lab A2L" ends with it.
 */
function onA2l(item: QueueItem): boolean {
  return [item.sliced_for_model, item.target_model].some((model) => /(^|\s)A2L$/i.test(model?.trim() ?? ''))
}

/**
 * A global tray id of Bambuddy's `ams_mapping` as Bambuddy names the tray, by the rules
 * of `backend/scadbuddy/printers/ams_mapping.py` (`_slot`): `ams_id * 4 + slot` for an
 * AMS, the AMS-HT's own id (128 and up, one slot), the A2L's AMS-Lite (`AMS_LITE_ID`
 * 16) by its local slot 0-3, and -1 for an external spool or a slot no tray feeds. The
 * flat id alone cannot tell the AMS-Lite's slots from the first AMS's, so `a2l` says
 * which printer the item is for.
 */
function trayOfGlobalId(id: number, a2l: boolean): string {
  if (id < 0) return 'External spool, or no tray'
  if (id >= 128) return `${amsUnitLabel(id)} · slot 1`
  if (a2l && id < 4) return `${amsUnitLabel(AMS_LITE_UNIT)} · slot ${id + 1}`
  return `${amsUnitLabel(Math.floor(id / 4))} · slot ${(id % 4) + 1}`
}

/**
 * #3744 — one item of Bambuddy's print queue, whoever queued it: Bambuddy's fields for
 * it and, for a job ScadBuddy queued, its output, template, revision, parameters, our
 * render and the tray of each filament slot. Bambuddy has no page per queue item, so the
 * onward link is its queue page. Once ScadBuddy has linked the item's archive, the page
 * goes on to that print (`/prints/{archive_id}`); an item Bambuddy no longer has is said
 * to be gone, not an error.
 */
export function QueueItemPage() {
  const { itemId = '' } = useParams()
  const valid = ITEM_ID.test(itemId)
  const state = useAsync(
    () => (valid ? api.getQueueItem(Number(itemId)) : Promise.resolve(null)),
    [itemId, valid],
  )
  const item = state.data

  if (state.loading) {
    return (
      <div role="status" aria-label="Loading the queue item" className="mx-auto max-w-6xl px-4 py-6">
        <span className="sr-only">Loading the queue item</span>
        <Skeleton className="h-3 w-40" />
        <Skeleton className="mt-3 mb-5 h-6 w-64" />
        <Skeleton className="h-48 rounded-xl" />
      </div>
    )
  }
  if (!valid || isGone(state.error)) return <Gone itemId={itemId} />
  if (state.error || !item) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-base font-medium">The queue item could not be read</h1>
        <p className="mt-2 text-sm text-muted-foreground">{state.error?.message}</p>
        <Button asChild variant="outline" className="mt-4">
          <Link to="/prints">Back to the prints</Link>
        </Button>
      </div>
    )
  }
  // Printed and linked: its print is the page to read now.
  if (item.scadbuddy?.archive_id != null) return <Navigate replace to={printPath(item.scadbuddy.archive_id)} />
  return <ItemView item={item} />
}

function Gone({ itemId }: { itemId: string }) {
  return (
    <div className="mx-auto max-w-lg px-4 py-16 text-center">
      <h1 className="text-base font-medium">This queue item is gone</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Bambuddy no longer has queue item {itemId}. Someone deleted it, or deleted its archive.
      </p>
      <Button asChild variant="outline" className="mt-4">
        <Link to="/prints">Back to the prints</Link>
      </Button>
    </div>
  )
}

function itemName(item: QueueItem): string {
  return item.archive_name ?? item.library_file_name ?? `Queue item ${item.id}`
}

function ItemView({ item }: { item: QueueItem }) {
  const base = bambuddyBase()
  const job = item.scadbuddy ?? null
  // The output ScadBuddy queued, read once for its name, revision, parameters and colours.
  // A failed read is said in its section, with a retry; the rest of the page stands.
  const outputRead = useAsync(
    async () => (job?.output_id ? await api.getOutput(job.output_id) : null),
    [job?.output_id],
  )
  const output = outputRead.data
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <nav aria-label="Breadcrumb" className="mb-2 flex items-baseline gap-1.5 text-xs text-muted-foreground">
          <Link to="/prints" className="hover:text-foreground">
            Prints
          </Link>
          <ChevronRightIcon aria-hidden="true" className="size-3 self-center" />
          <span>Queue item {item.id}</span>
        </nav>
        <header className="mb-5 flex flex-wrap items-center gap-3">
          <JobPicture src={item.picture} className="size-16" />
          <h1 className="text-xl font-semibold tracking-tight">{itemName(item)}</h1>
          {item.status && <PrintStatus status={item.status} />}
          {job && <QueuedFromScadBuddy job={job} />}
          {base && (
            <div className="ml-auto">
              <Button variant="outline" onClick={() => openExternal(`${base}/queue`)}>
                <ExternalLinkIcon aria-hidden="true" />
                Open Bambuddy's queue
              </Button>
            </div>
          )}
        </header>

        {item.archive_id != null && <Printed item={item} base={base} />}

        <div className="grid gap-4 lg:grid-cols-2">
          <QueueSection item={item} />
          {job && (
            <ScadBuddySection
              job={job}
              output={output ?? null}
              error={outputRead.error ?? null}
              onRetry={() => outputRead.refresh()}
            />
          )}
          {item.ams_mapping && item.ams_mapping.length > 0 && (
            <SlotsSection item={item} colours={output?.colors ?? []} a2l={onA2l(item)} />
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * Bambuddy archived the item's print, and ScadBuddy has no print page for it: the item
 * is not ScadBuddy's, or ScadBuddy has not linked the archive yet. Bambuddy's own
 * archive page needs a version check (`bambuddy/printers_now.py` `has_archive_page`),
 * which would be a second call, so the link is its archives page.
 */
function Printed({ item, base }: { item: QueueItem; base: string | null }) {
  return (
    <Alert role="status" aria-label="Printed" className="mb-4">
      <AlertTitle>Printed</AlertTitle>
      <AlertDescription>
        <p>
          Bambuddy keeps this print as archive {item.archive_id}
          {item.scadbuddy ? '. Its ScadBuddy print page opens once ScadBuddy links the archive.' : '.'}
          {base && (
            <>
              {' '}
              <a href={`${base}/archives`} {...NEW_TAB} className="font-medium text-primary underline underline-offset-4">
                Bambuddy's archives
              </a>
            </>
          )}
        </p>
      </AlertDescription>
    </Alert>
  )
}

function Section({ title, children, wide }: { title: string; children: ReactNode; wide?: boolean }) {
  const heading = `queue-section-${title.toLowerCase().replace(/\W+/g, '-')}`
  return (
    <Card role="region" aria-labelledby={heading} className={cn('min-w-0 gap-3', wide && 'lg:col-span-2')}>
      <CardHeader>
        <CardTitle>
          <h2 id={heading} className="text-sm font-medium">
            {title}
          </h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="min-w-0">{children}</CardContent>
    </Card>
  )
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  if (value === null || value === undefined || value === '') return null
  return (
    <>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="sb-num text-[13px]">{value}</dd>
    </>
  )
}

function QueueSection({ item }: { item: QueueItem }) {
  const printer = item.printer_name ?? (item.target_model ? `Any ${item.target_model}` : 'No printer yet')
  const filament = [
    item.filament_used_grams ? `${Math.round(item.filament_used_grams)} g` : null,
    item.filament_type,
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <Section title="In Bambuddy's queue">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <Fact label="Status" value={item.status ? statusLabel(item.status) : null} />
        <Fact label="Position" value={item.position} />
        <Fact label="Printer" value={printer} />
        <Fact label="Sliced for" value={item.sliced_for_model} />
        <Fact label="Waiting because" value={item.waiting_reason} />
        <Fact label="Error" value={item.error_message} />
        <Fact label="Scheduled" value={when(item.scheduled_time)} />
        <Fact label="Start" value={item.manual_start ? 'Waits for a manual start' : null} />
        <Fact label="Estimated time" value={item.print_time_seconds ? formatDuration(item.print_time_seconds) : null} />
        <Fact
          label="Estimated filament"
          value={
            filament ? (
              <span className="inline-flex items-center gap-1.5">
                {item.filament_color && (
                  <span
                    aria-hidden="true"
                    className="inline-block size-3 rounded-sm border"
                    style={{ backgroundColor: item.filament_color }}
                  />
                )}
                {filament}
              </span>
            ) : null
          }
        />
        <Fact label="Plate" value={item.plate_id} />
        <Fact label="Build plate" value={item.bed_type} />
        <Fact label="Batch" value={item.batch_name} />
        <Fact label="Queued" value={when(item.created_at)} />
        <Fact label="Started" value={when(item.started_at)} />
        <Fact label="Finished" value={when(item.completed_at)} />
      </dl>
      {item.filament_short && <p className="mt-2 text-xs text-warn">Not enough filament loaded for this print.</p>}
    </Section>
  )
}

/**
 * What ScadBuddy queued: the output (its template, revision and parameters) or the
 * library file. When the output cannot be read, the section says so and offers to read
 * it again, rather than show no parameters.
 */
function ScadBuddySection({
  job,
  output: data,
  error,
  onRetry,
}: {
  job: ScadBuddyJob
  output: Output | null
  error: Error | null
  onRetry: () => void
}) {
  const link = 'font-medium text-primary underline underline-offset-4'
  return (
    <Section title="From ScadBuddy">
      {error && (
        <Alert role="alert" className="mb-3 border-warn/40 bg-warn/10 text-warn">
          <TriangleAlertIcon aria-hidden="true" />
          <AlertDescription className="text-warn">
            <p>The output could not be read: {error.message}</p>
            <Button variant="outline" size="sm" className="mt-2" onClick={onRetry}>
              Try again
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        {job.output_id && (
          <Fact
            label="Output"
            value={
              <Link to={editPath(job.output_id)} className={link}>
                {data?.name ?? job.output_id}
              </Link>
            }
          />
        )}
        {job.slug && (
          <Fact
            label="Template"
            value={
              <Link to={modelPath(job.slug)} className={link}>
                {job.slug}
              </Link>
            }
          />
        )}
        {job.slug && data?.model_version && (
          <Fact
            label="Revision"
            value={
              <Link to={modelPath(job.slug, 'versions')} className={link}>
                {data.model_version.slice(0, 7)}
              </Link>
            }
          />
        )}
        {job.library_file_id != null && (
          <Fact
            label="Library file"
            value={
              <Link to={`/library?file=${job.library_file_id}`} className={link}>
                File {job.library_file_id}
              </Link>
            }
          />
        )}
      </dl>
      {data && Object.keys(data.params ?? {}).length > 0 && (
        <Card size="sm" className="mt-3 gap-0 py-0">
          <ParamsTable label="Parameters" params={data.params ?? {}} />
        </Card>
      )}
      {job.output_id && (
        <Button asChild variant="outline" className="mt-3">
          <Link to={editPath(job.output_id)}>
            <SlidersHorizontalIcon aria-hidden="true" />
            Customize from this
          </Link>
        </Button>
      )}
    </Section>
  )
}

/**
 * The tray each filament slot of the plate draws from, as the item's `ams_mapping`
 * names it, beside the output's colour for that slot when ScadBuddy made it.
 */
function SlotsSection({ item, colours, a2l }: { item: QueueItem; colours: readonly string[]; a2l: boolean }) {
  return (
    <Section title="Filament slots">
      <ol className="space-y-1 text-[13px]">
        {(item.ams_mapping ?? []).map((tray, index) => (
          <li key={index} className="flex items-center gap-2">
            {colours[index] && (
              <span
                aria-hidden="true"
                className="inline-block size-3 rounded-sm border"
                style={{ backgroundColor: colours[index] }}
              />
            )}
            <span className="text-muted-foreground">Slot {index + 1}</span>
            <span className="sb-num">{trayOfGlobalId(tray, a2l)}</span>
          </li>
        ))}
      </ol>
      {item.ams_mapping && item.ams_mapping.some((tray) => tray < 0) && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
          <TriangleAlertIcon aria-hidden="true" className="size-3.5" />
          Bambuddy gives no tray for an external spool.
        </p>
      )}
    </Section>
  )
}
