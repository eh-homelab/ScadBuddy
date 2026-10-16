import { ChevronsUpDownIcon, SearchIcon, XIcon } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { fuzzyScore } from '../../lib/fuzzy'
import { useLatest } from '../../lib/useLatest'
import { clearPrintFilters, isFiltered, type PrintsQuery, type PrintsView } from '../../lib/printsQuery'
import { Hint } from '../Hint'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
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
  /** #1755 — the name of the library file `query.file` names, once a print of it says. */
  fileName?: string | null
}

/**
 * #310 — template, status, printer, date range and text filters over the print
 * history, and the Cards/List view: the catalogue's filter bar (#276, #278) for prints.
 */
export function PrintFilterBar({ query, onChange, templates, printers, fileName }: Props) {
  const [text, setText] = useState(query.q)
  const sent = useRef(query.q)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const latest = useLatest(query)
  const id = useId()

  // A search that changed from outside (Clear filters, back/forward) replaces the text,
  // and drops a pending one that belongs to the query that no longer exists.
  useEffect(() => {
    if (query.q === sent.current) return
    clearTimeout(timer.current)
    sent.current = query.q
    setText(query.q)
  }, [query.q])

  // The debounce only ever changes `q`, so any other field changing means the query was
  // set from somewhere else: Back/Forward, a link, Clear filters. A search still pending
  // belongs to a query that no longer exists, so it is dropped rather than written over
  // the new URL, as the catalogue's filters do (#276). A change made here carries the
  // pending text in `q` first; see `commit`.
  const others = [query.slug, query.status, query.printer, query.from, query.to, query.file, query.view].join(
    '\u0000',
  )
  const seenOthers = useRef(others)
  useEffect(() => {
    if (others === seenOthers.current) return
    seenOthers.current = others
    clearTimeout(timer.current)
    sent.current = latest.current.q
    setText(latest.current.q)
  }, [others, latest])

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
        <div className="relative min-w-48 flex-1">
          <SearchIcon
            aria-hidden="true"
            className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            type="search"
            aria-label="Search prints"
            placeholder="Search names and parameters"
            value={text}
            onChange={(event) => search(event.target.value)}
            className="pl-8"
          />
        </div>
        <ToggleGroup
          type="single"
          aria-label="View"
          variant="outline"
          size="sm"
          spacing={0}
          value={query.view}
          // Radix answers '' for a click on the pressed one: one stays pressed.
          onValueChange={(next) => next && commit({ view: next as PrintsView })}
        >
          {VIEWS.map((option) => (
            <ToggleGroupItem key={option.value} value={option.value} className="px-3 text-xs">
              {option.label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted-foreground">
        {templateOptions && (
          <span className="flex items-center gap-1.5">
            <Label id={`${id}-template`} className="text-xs font-normal text-muted-foreground">
              Template
            </Label>
            <TemplatePicker
              labelledBy={`${id}-template`}
              value={query.slug}
              templates={templateOptions}
              // A print is of a template's output or of a library file: one replaces the other.
              onChange={(slug) => commit({ slug, file: '' })}
            />
          </span>
        )}
        {templateOptions && query.file && !query.slug && (
          <Badge
            variant="outline"
            role="group"
            aria-label="Library file"
            className="h-7 max-w-64 gap-1 pr-0.5 pl-2.5 text-xs font-normal"
          >
            <span className="text-muted-foreground">File</span>
            <Hint content={fileName}>
              <span className="truncate">{fileName ?? 'Bambuddy library file'}</span>
            </Hint>
            <Button
              variant="ghost"
              size="icon-xs"
              aria-label="Show every file's prints"
              onClick={() => commit({ file: '' })}
              className="rounded-full"
            >
              <XIcon aria-hidden="true" />
            </Button>
          </Badge>
        )}
        <FilterSelect
          id={`${id}-status`}
          label="Status"
          value={query.status}
          onChange={(status) => commit({ status })}
          options={[
            { value: '', label: 'Any status' },
            ...(query.status && !statuses.includes(query.status) ? [{ value: query.status, label: query.status }] : []),
            ...statuses.map((status) => ({ value: status, label: STATUS_LABELS[status] ?? status })),
          ]}
        />
        <FilterSelect
          id={`${id}-printer`}
          label="Printer"
          value={query.printer}
          onChange={(printer) => commit({ printer })}
          options={[
            { value: '', label: 'Any printer' },
            ...printerIds.map((printerId) => ({
              value: String(printerId),
              label: printers.get(printerId) ?? printerLabel(printerId),
            })),
          ]}
        />
        <span className="flex items-center gap-1.5">
          <Label htmlFor={`${id}-from`} className="text-xs font-normal text-muted-foreground">
            From
          </Label>
          <Input
            id={`${id}-from`}
            type="date"
            value={query.from}
            max={query.to || undefined}
            onChange={(event) => commit({ from: event.target.value })}
            className="h-7 w-auto"
          />
        </span>
        <span className="flex items-center gap-1.5">
          <Label htmlFor={`${id}-to`} className="text-xs font-normal text-muted-foreground">
            To
          </Label>
          <Input
            id={`${id}-to`}
            type="date"
            value={query.to}
            min={query.from || undefined}
            onChange={(event) => commit({ to: event.target.value })}
            className="h-7 w-auto"
          />
        </span>
        {isFiltered(query) && (
          <Button size="sm" variant="ghost" onClick={() => commit(clearPrintFilters(query))}>
            <XIcon aria-hidden="true" />
            Clear filters
          </Button>
        )}
      </div>
    </div>
  )
}

/** The Select's value for "any": Radix keeps '' for "nothing chosen". */
const ANY = 'any'

/** A filter's select: `''` is its "any" choice, as in the URL. */
function FilterSelect({
  id,
  label,
  value,
  options,
  onChange,
}: {
  id: string
  label: string
  value: string
  options: Array<{ value: string; label: string }>
  onChange: (value: string) => void
}) {
  return (
    <span className="flex items-center gap-1.5">
      <Label id={id} className="text-xs font-normal text-muted-foreground">
        {label}
      </Label>
      <Select value={value || ANY} onValueChange={(next) => onChange(next === ANY ? '' : next)}>
        <SelectTrigger size="sm" aria-labelledby={id} className="max-w-56 text-[0.8rem] text-foreground">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value || ANY} value={option.value || ANY}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </span>
  )
}

/** #3653 — the templates, searchable: a long catalogue is more than a list to scroll. */
function TemplatePicker({
  labelledBy,
  value,
  templates,
  onChange,
}: {
  labelledBy: string
  value: string
  templates: Array<{ slug: string; name: string }>
  onChange: (slug: string) => void
}) {
  const [open, setOpen] = useState(false)
  const chosen = templates.find((template) => template.slug === value)
  function choose(slug: string) {
    setOpen(false)
    if (slug !== value) onChange(slug)
  }
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          role="combobox"
          aria-expanded={open}
          aria-labelledby={labelledBy}
          data-value={value}
          className="w-auto max-w-56 justify-between gap-2 font-normal"
        >
          <span className="min-w-0 truncate">{chosen?.name ?? 'All templates'}</span>
          <ChevronsUpDownIcon aria-hidden="true" className="opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(18rem,calc(100vw-2rem))] gap-0 p-0">
        <Command filter={(_value, search, keywords) => fuzzyScore((keywords ?? []).join(' '), search)}>
          <CommandInput placeholder="Search templates…" aria-label="Search templates" />
          <CommandList>
            <CommandEmpty>No template matches.</CommandEmpty>
            <CommandGroup>
              <CommandItem value="all-templates" keywords={['All templates']} data-checked={value === ''} onSelect={() => choose('')}>
                All templates
              </CommandItem>
              {templates.map((template) => (
                <CommandItem
                  key={template.slug}
                  value={`template-${template.slug}`}
                  keywords={[template.name, template.slug]}
                  data-checked={value === template.slug}
                  onSelect={() => choose(template.slug)}
                >
                  <span className="truncate">{template.name}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}
