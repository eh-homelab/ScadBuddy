import { PaperclipIcon, PlayIcon } from 'lucide-react'
import { Link } from 'react-router'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { Hint } from '../Hint'
import { libraryOptionsScope, readPrintProgress } from '../../lib/printSource'
import type { PrintSummary } from '../../api/types'
import { formatDuration, formatValue } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import type { PrintsView } from '../../lib/printsQuery'
import { libraryPrintsPath, printerLabel, printLabel, printPath } from './prints'
import { PrintStatus } from './PrintStatus'
import { DELETED_STATUS } from './status'

/** How many changed parameters an item lists before "+N more". */
const PARAMS_SHOWN = 4

interface Props {
  print: PrintSummary
  view: PrintsView
  /** The template's name, shown on the global history only. */
  templateName?: string
  onOpenMedia: (print: PrintSummary) => void
}

/**
 * One print in the history (#310). The name is the item's one link, and its ::after
 * stretches over the whole item, so a click anywhere opens the print (#311); the image
 * sits above it and opens the lightbox (#275) instead.
 */
export function PrintItem({ print, view, templateName, onOpenMedia }: Props) {
  const label = printLabel(print)
  const subject = subjectOf(print)
  const raised = 'relative z-10'
  const cards = view === 'cards'
  return (
    <li data-print={print.archive_id} className="min-w-0">
      <Card
        className={cn(
          'relative h-full gap-0 py-0 transition-shadow hover:ring-foreground/20 hover:shadow-sm',
          cards ? 'flex-col' : 'flex-row items-start gap-3 p-2.5',
        )}
      >
        <div className={cards ? 'p-3 pb-0' : 'w-24 shrink-0 sm:w-28'}>
          <Cover print={print} label={label} className={raised} onOpen={() => onOpenMedia(print)} />
        </div>

        <div className={`min-w-0 flex-1 ${cards ? 'px-3 pt-2.5 pb-3' : ''}`}>
          <div className="flex items-center gap-2">
            <h2 className="min-w-0 truncate text-sm font-medium">
              <Link
                to={printPath(print.archive_id)}
                className="outline-none after:absolute after:inset-0 after:rounded-xl after:content-[''] focus-visible:after:ring-3 focus-visible:after:ring-ring/50"
              >
                {label}
              </Link>
            </h2>
            <PrintStatus status={print.status} />
          </div>
          {templateName &&
            (print.library_file_id !== null ? (
              // #1755 — every print of the file, above the item's own link.
              <p className="mt-0.5 truncate text-xs text-muted-foreground">
                <Hint content="Every print of this file">
                  <Link
                    to={libraryPrintsPath(print.library_file_id)}
                    className={`${raised} underline-offset-4 hover:text-foreground hover:underline`}
                  >
                    {templateName}
                  </Link>
                </Hint>
              </p>
            ) : (
              <p className="mt-0.5 truncate text-xs text-muted-foreground">{templateName}</p>
            ))}
          <Facts print={print} />
          {print.status === 'printing' && subject !== null && (
            <PrintingNow subject={subject} named={print.printer_name !== null || print.printer_id !== null} />
          )}
          <ParamsDiff diff={print.params_diff} />
        </div>
      </Card>
    </li>
  )
}

function Cover({
  print,
  label,
  className,
  onOpen,
}: {
  print: PrintSummary
  label: string
  className: string
  onOpen: () => void
}) {
  const overlay = 'border-transparent bg-black/65 text-white'
  const badges = (
    <span className="absolute right-1.5 bottom-1.5 flex gap-1">
      {print.has_timelapse && (
        <Hint content="Timelapse">
          <Badge role="img" aria-label="Has a timelapse" className={cn('size-5 px-0', overlay)}>
            <PlayIcon aria-hidden="true" className="fill-current" />
          </Badge>
        </Hint>
      )}
      {print.attachment_count > 0 && (
        <Hint content="Attachments">
          <Badge
            aria-label={`${print.attachment_count} ${print.attachment_count === 1 ? 'attachment' : 'attachments'}`}
            className={cn('sb-num gap-0.5 px-1.5 text-[11px]', overlay)}
          >
            <PaperclipIcon aria-hidden="true" />
            {print.attachment_count}
          </Badge>
        </Hint>
      )}
    </span>
  )
  const frame = 'relative block aspect-[4/3] w-full overflow-hidden rounded-lg bg-muted'
  if (!print.cover) {
    return (
      <div
        data-testid="print-cover"
        className={`${className} ${frame} flex items-center justify-center p-2 text-center text-[11px] text-muted-foreground`}
      >
        {print.status === DELETED_STATUS ? 'Archive deleted in Bambuddy' : 'No image'}
        {badges}
      </div>
    )
  }
  return (
    <Button
      variant="ghost"
      aria-label={`Open media of ${label}`}
      onClick={onOpen}
      className={cn(className, frame, 'h-auto cursor-zoom-in p-0 hover:bg-muted active:not-aria-[haspopup]:translate-y-0')}
    >
      <img
        src={print.cover.url}
        alt=""
        loading="lazy"
        className={`size-full ${print.cover.kind === 'photo' ? 'object-cover' : 'object-contain'}`}
      />
      {badges}
    </Button>
  )
}

/** Printer, day, duration and filament: whatever the archive has. */
function Facts({ print }: { print: PrintSummary }) {
  const when = print.started_at ?? print.completed_at
  const facts = [
    print.printer_name ?? (print.printer_id !== null ? printerLabel(print.printer_id) : null),
    print.actual_time_seconds !== null ? formatDuration(print.actual_time_seconds) : null,
    print.filament_used_grams !== null ? `${print.filament_used_grams.toFixed(1)} g` : null,
    print.run_count > 1 ? `${print.run_count} runs` : null,
  ].filter((fact): fact is string => fact !== null)
  if (!when && facts.length === 0) return null
  return (
    <p className="sb-num mt-1 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
      {when && (
        <time dateTime={when}>
          {new Date(when).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}
        </time>
      )}
      {facts.map((fact) => (
        <span key={fact}>{fact}</span>
      ))}
    </p>
  )
}

/**
 * Where a print in progress is, from the output's progress read (#89), live on
 * `print:<output id>`. That read follows the output's latest send, which is this print
 * while it is the one printing.
 */
/** #1751 — what a print's live progress is read by: its output, or its library file. */
function subjectOf(print: PrintSummary): string | null {
  if (print.output_id !== null) return print.output_id
  return print.library_file_id !== null ? libraryOptionsScope(print.library_file_id) : null
}

function PrintingNow({ subject, named }: { subject: string; named: boolean }) {
  const { data } = useAsync(() => readPrintProgress(subject), [subject], [`print:${subject}`])
  const copies = data?.copies_detail ?? []
  const copy = copies.find((c) => c.stage === 'running') ?? copies[0]
  if (!copy) return null
  const text = copy.message ?? copy.waiting_reason
  // The list names the archive's printer (#2363); the read's is only for a print with none.
  const printer = named ? null : copy.printer_name
  if (!printer && !text) return null
  return (
    <p aria-live="polite" className="mt-1 flex flex-wrap gap-x-2 text-xs">
      {printer && <span className="text-muted-foreground">{printer}</span>}
      {text && <span>{text}</span>}
    </p>
  )
}

/** The parameters that differ from the template's defaults. */
function ParamsDiff({ diff }: { diff: PrintSummary['params_diff'] }) {
  if (!diff) return null
  const entries = Object.entries(diff)
  if (entries.length === 0) {
    return <p className="mt-2 text-xs text-muted-foreground">Template defaults</p>
  }
  const rest = entries.length - PARAMS_SHOWN
  return (
    // #2457 — "+N more" sits beside the list, not in it: a <dl> holds only term groups.
    <div className="mt-2 flex flex-wrap items-start gap-1 text-[11px]">
      <dl className="flex min-w-0 flex-wrap gap-1" aria-label="Changed parameters">
        {entries.slice(0, PARAMS_SHOWN).map(([name, value]) => (
          <div key={name} className="flex max-w-full min-w-0 gap-1 rounded-md bg-muted px-1.5 py-0.5">
            <dt className="text-muted-foreground">{name}</dt>
            <dd className="sb-num truncate">{formatValue(value)}</dd>
          </div>
        ))}
      </dl>
      {rest > 0 && (
        <p className="px-1 py-0.5 text-muted-foreground">
          +{rest} more
        </p>
      )}
    </div>
  )
}
