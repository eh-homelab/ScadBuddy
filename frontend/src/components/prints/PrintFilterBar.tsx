import { useEffect, useRef, useState } from 'react'
import { useLatest } from '../../lib/useLatest'
import { clearPrintFilters, isFiltered, type PrintsQuery, type PrintsView } from '../../lib/printsQuery'
import { Button } from '../ui/Button'
import { printerLabel } from './prints'
import { STATUS_LABELS } from './status'

const SEARCH_DEBOUNCE_MS = 250

const VIEWS: Array<{ value: PrintsView; label: string }> = [
  { value: 'cards', label: 'Cards' },
  { value: 'list', label: 'List' },
]

interface Props {
  query: PrintsQuery
  /** `replace` is set for search keystrokes, so typing does not fill the history. */
  onChange: (next: PrintsQuery, options?: { replace?: boolean }) => void
  /** The template picker's choices; absent on a template's own Prints tab. */
  templates?: Array<{ slug: string; name: string }>
  /** Printers to offer by id, with Bambuddy's name where it gave one, besides the one
   * selected. */
  printers: Map<number, string | null>
}

/**
 * #310 — template, status, printer, date range and text filters over the print
 * history, and the Cards/List view: the catalogue's filter bar (#276, #278) for prints.
 */
export function PrintFilterBar({ query, onChange, templates, printers }: Props) {
  const [text, setText] = useState(query.q)
  const sent = useRef(query.q)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const latest = useLatest(query)

  // A search that changed from outside (Clear filters, back/forward) replaces the text,
  // and drops a pending one that belongs to the query that no longer exists.
  useEffect(() => {
    if (query.q === sent.current) return
    clearTimeout(timer.current)
    sent.current = query.q
    setText(query.q)
  }, [query.q])

  useEffect(() => () => clearTimeout(timer.current), [])

  function search(value: string) {
    setText(value)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      sent.current = value
      onChange({ ...latest.current, q: value }, { replace: true })
    }, SEARCH_DEBOUNCE_MS)
  }

  /** A change made here takes whatever is typed with it, pending or not. */
  function commit(next: Partial<PrintsQuery>) {
    clearTimeout(timer.current)
    const merged = { ...query, q: text, ...next }
    sent.current = merged.q
    setText(merged.q)
    onChange(merged)
  }

  const printerIds = [...new Set([...printers.keys(), ...(query.printer ? [Number(query.printer)] : [])])].sort(
    (a, b) => a - b,
  )
  const statuses = Object.keys(STATUS_LABELS)
  const templateOptions =
    templates && query.slug && !templates.some((t) => t.slug === query.slug)
      ? [...templates, { slug: query.slug, name: query.slug }]
      : templates

  return (
    <div className="mb-4 space-y-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="search"
          aria-label="Search prints"
          placeholder="Search names and parameters"
          value={text}
          onChange={(event) => search(event.target.value)}
          className="sb-field h-8 min-w-48 flex-1"
        />
        <div role="group" aria-label="View" className="flex rounded-[6px] border border-line bg-surface p-0.5">
          {VIEWS.map((option) => (
            <button
              key={option.value}
              type="button"
              aria-pressed={query.view === option.value}
              onClick={() => commit({ view: option.value })}
              className={`h-6 rounded-[4px] px-2.5 text-[12px] transition-colors ${
                query.view === option.value ? 'bg-surface-3 text-ink' : 'text-muted hover:text-ink'
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-[12px] text-muted">
        {templateOptions && (
          <label className="flex items-center gap-1.5">
            Template
            <select
              value={query.slug}
              onChange={(event) => commit({ slug: event.target.value })}
              className="sb-field h-8 w-auto max-w-56 cursor-pointer"
            >
              <option value="">All templates</option>
              {templateOptions.map((template) => (
                <option key={template.slug} value={template.slug}>
                  {template.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="flex items-center gap-1.5">
          Status
          <select
            value={query.status}
            onChange={(event) => commit({ status: event.target.value })}
            className="sb-field h-8 w-auto cursor-pointer"
          >
            <option value="">Any status</option>
            {query.status && !statuses.includes(query.status) && (
              <option value={query.status}>{query.status}</option>
            )}
            {statuses.map((status) => (
              <option key={status} value={status}>
                {STATUS_LABELS[status]}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          Printer
          <select
            value={query.printer}
            onChange={(event) => commit({ printer: event.target.value })}
            className="sb-field h-8 w-auto cursor-pointer"
          >
            <option value="">Any printer</option>
            {printerIds.map((id) => (
              <option key={id} value={String(id)}>
                {printers.get(id) ?? printerLabel(id)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          From
          <input
            type="date"
            value={query.from}
            max={query.to || undefined}
            onChange={(event) => commit({ from: event.target.value })}
            className="sb-field h-8 w-auto"
          />
        </label>
        <label className="flex items-center gap-1.5">
          To
          <input
            type="date"
            value={query.to}
            min={query.from || undefined}
            onChange={(event) => commit({ to: event.target.value })}
            className="sb-field h-8 w-auto"
          />
        </label>
        {isFiltered(query) && (
          <Button size="sm" variant="ghost" onClick={() => commit(clearPrintFilters(query))}>
            Clear filters
          </Button>
        )}
      </div>
    </div>
  )
}
