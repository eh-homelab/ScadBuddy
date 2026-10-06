import { Link } from 'react-router'
import { api } from '../../api/client'
import type { PrintSummary } from '../../api/types'
import { formatDuration, formatValue } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import type { PrintsView } from '../../lib/printsQuery'
import { printerLabel, printLabel, printPath } from './prints'
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
  const raised = 'relative z-10'
  const cards = view === 'cards'
  return (
    <li
      data-print={print.archive_id}
      className={`relative rounded-[6px] border border-line bg-surface transition-colors hover:border-line-strong ${
        cards ? 'flex flex-col' : 'flex items-start gap-3 p-2.5'
      }`}
    >
      <div className={cards ? 'p-3 pb-0' : 'w-24 shrink-0 sm:w-28'}>
        <Cover print={print} label={label} className={raised} onOpen={() => onOpenMedia(print)} />
      </div>

      <div className={`min-w-0 flex-1 ${cards ? 'px-3 pt-2.5 pb-3' : ''}`}>
        <div className="flex items-center gap-2">
          <h2 className="min-w-0 truncate text-[14px] font-medium">
            <Link
              to={printPath(print.archive_id)}
              className="outline-none after:absolute after:inset-0 after:rounded-[6px] after:content-[''] focus-visible:after:ring-2 focus-visible:after:ring-accent"
            >
              {label}
            </Link>
          </h2>
          <PrintStatus status={print.status} />
        </div>
        {templateName && <p className="mt-0.5 truncate text-[12px] text-muted">{templateName}</p>}
        <Facts print={print} />
        {print.status === 'printing' && print.output_id !== null && (
          <PrintingNow outputId={print.output_id} named={print.printer_name !== null} />
        )}
        <ParamsDiff diff={print.params_diff} />
      </div>
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
  const badges = (
    <span className="pointer-events-none absolute right-1.5 bottom-1.5 flex gap-1">
      {print.has_timelapse && (
        <span
          role="img"
          aria-label="Has a timelapse"
          title="Timelapse"
          className="flex size-5 items-center justify-center rounded-full bg-black/65 text-white"
        >
          <svg viewBox="0 0 16 16" className="size-2.5" fill="currentColor" aria-hidden>
            <path d="M4 2.5v11l9-5.5z" />
          </svg>
        </span>
      )}
      {print.attachment_count > 0 && (
        <span
          aria-label={`${print.attachment_count} ${print.attachment_count === 1 ? 'attachment' : 'attachments'}`}
          title="Attachments"
          className="sb-num flex h-5 items-center gap-0.5 rounded-full bg-black/65 px-1.5 text-[11px] text-white"
        >
          <svg viewBox="0 0 16 16" className="size-2.5" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
            <path d="M10.5 4.5 5.8 9.2a1.4 1.4 0 0 0 2 2l5-5a2.8 2.8 0 0 0-4-4l-5 5a4.2 4.2 0 0 0 6 6l4-4" />
          </svg>
          {print.attachment_count}
        </span>
      )}
    </span>
  )
  const frame = 'relative block aspect-[4/3] w-full overflow-hidden rounded-[4px] bg-surface-2'
  if (!print.cover) {
    return (
      <div
        data-testid="print-cover"
        className={`${className} ${frame} flex items-center justify-center p-2 text-center text-[11px] text-faint`}
      >
        {print.status === DELETED_STATUS ? 'Archive deleted in Bambuddy' : 'No image'}
        {badges}
      </div>
    )
  }
  return (
    <button
      type="button"
      aria-label={`Open media of ${label}`}
      onClick={onOpen}
      className={`${className} ${frame} cursor-zoom-in outline-none focus-visible:ring-2 focus-visible:ring-accent`}
    >
      <img
        src={print.cover.url}
        alt=""
        loading="lazy"
        className={`size-full ${print.cover.kind === 'photo' ? 'object-cover' : 'object-contain'}`}
      />
      {badges}
    </button>
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
    <p className="sb-num mt-1 flex flex-wrap gap-x-3 text-[12px] text-muted">
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
function PrintingNow({ outputId, named }: { outputId: string; named: boolean }) {
  const { data } = useAsync(() => api.getPrintProgress(outputId), [outputId], [`print:${outputId}`])
  const copies = data?.copies_detail ?? []
  const copy = copies.find((c) => c.stage === 'running') ?? copies[0]
  if (!copy) return null
  const text = copy.message ?? copy.waiting_reason
  // The list names the archive's printer; the read's is only a fallback.
  const printer = named ? null : copy.printer_name
  if (!printer && !text) return null
  return (
    <p aria-live="polite" className="mt-1 flex flex-wrap gap-x-2 text-[12px] text-ink">
      {printer && <span className="text-muted">{printer}</span>}
      {text && <span>{text}</span>}
    </p>
  )
}

/** The parameters that differ from the template's defaults. */
function ParamsDiff({ diff }: { diff: PrintSummary['params_diff'] }) {
  if (!diff) return null
  const entries = Object.entries(diff)
  if (entries.length === 0) {
    return <p className="mt-2 text-[12px] text-faint">Template defaults</p>
  }
  const rest = entries.length - PARAMS_SHOWN
  return (
    <dl className="mt-2 flex flex-wrap gap-1 text-[11px]" aria-label="Changed parameters">
      {entries.slice(0, PARAMS_SHOWN).map(([name, value]) => (
        <div key={name} className="flex max-w-full min-w-0 gap-1 rounded-[3px] bg-surface-2 px-1.5 py-0.5">
          <dt className="text-muted">{name}</dt>
          <dd className="sb-num truncate text-ink">{formatValue(value)}</dd>
        </div>
      ))}
      {rest > 0 && <div className="px-1 py-0.5 text-faint">+{rest} more</div>}
    </dl>
  )
}
