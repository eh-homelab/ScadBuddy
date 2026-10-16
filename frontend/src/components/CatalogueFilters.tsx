import { useEffect, useId, useRef, useState } from 'react'
import { activeDialog } from '../agent/dom'
import { useLatest } from '../agent/useAgentHandlers'
import {
  clearFilters,
  type CatalogueOrigin,
  type CatalogueQuery,
  type CatalogueSort,
  type CatalogueView,
} from '../lib/catalogueQuery'
import { ChevronRightIcon, SearchIcon, XIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'

const SEARCH_DEBOUNCE_MS = 250

const ORIGINS: Array<{ value: CatalogueOrigin; label: string }> = [
  { value: 'all', label: 'All' },
  { value: 'builtin', label: 'Built-in' },
  { value: 'mine', label: 'Mine' },
]

const VIEWS: Array<{ value: CatalogueView; label: string }> = [
  { value: 'cards', label: 'Cards' },
  { value: 'list', label: 'List' },
]

const SORTS: Array<{ value: CatalogueSort; label: string }> = [
  { value: 'updated', label: 'Recently updated' },
  { value: 'name', label: 'Name' },
]

interface Props {
  query: CatalogueQuery
  /** `replace` is set for search keystrokes, so typing does not fill the history. */
  onChange: (next: CatalogueQuery, options?: { replace?: boolean }) => void
  tags: Array<{ tag: string; count: number }>
  shown: number
  total: number
}

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
}

/** #276 — search, tag chips, origin and sort above the catalogue; #278 — the view. */
export function CatalogueFilters({ query, onChange, tags, shown, total }: Props) {
  const [text, setText] = useState(query.q)
  const sent = useRef(query.q)
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const latest = useLatest(query)
  const input = useRef<HTMLInputElement>(null)
  const [tagsOpen, setTagsOpen] = useState(false)
  const tagsId = useId()
  const sortId = useId()

  // A search that changed from outside (Clear filters, back/forward) replaces the text.
  useEffect(() => {
    if (query.q === sent.current) return
    clearTimeout(timer.current)
    sent.current = query.q
    setText(query.q)
  }, [query.q])

  // The debounce only ever changes `q`, so any other field changing means the query was
  // set from somewhere else — Clear filters, back/forward, a card's tag chip. A search
  // still pending then belongs to a query that no longer exists: firing it later would
  // re-apply what was just cleared. It is dropped and the box shows the query as it is.
  // (A change made here carries the pending text in `q` first; see `commit`.)
  const others = [query.origin, query.sort, query.view, ...query.tags].join('\u0000')
  const seenOthers = useRef(others)
  useEffect(() => {
    if (others === seenOthers.current) return
    seenOthers.current = others
    clearTimeout(timer.current)
    sent.current = latest.current.q
    setText(latest.current.q)
  }, [others, latest])

  useEffect(() => () => clearTimeout(timer.current), [])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return
      if (event.defaultPrevented || isTyping(event.target)) return
      // Nothing behind an open modal is reachable, the shortcut included.
      if (activeDialog()) return
      event.preventDefault()
      input.current?.focus()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  function search(value: string) {
    setText(value)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      sent.current = value
      onChange({ ...latest.current, q: value }, { replace: true })
    }, SEARCH_DEBOUNCE_MS)
  }

  /** A filter change made here: it takes whatever is typed with it, pending or not, so
   * the search is neither lost nor re-applied by the debounce afterwards. */
  function commit(next: CatalogueQuery) {
    clearTimeout(timer.current)
    sent.current = next.q
    setText(next.q)
    onChange(next)
  }

  function toggleTag(tag: string) {
    const tags = query.tags.includes(tag)
      ? query.tags.filter((t) => t !== tag)
      : [...query.tags, tag]
    commit({ ...query, q: text, tags })
  }

  const filtered = query.q.trim() !== '' || query.tags.length > 0 || query.origin !== 'all'

  const shownTags = tags.filter(({ tag }) => tagsOpen || query.tags.includes(tag))
  return (
    <div className="mb-4 space-y-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-48 flex-1">
          <SearchIcon aria-hidden className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            ref={input}
            type="search"
            aria-label="Search models"
            placeholder="Search models  /"
            value={text}
            onChange={(event) => search(event.target.value)}
            className="pl-8"
          />
        </div>
        <SegmentedGroup
          label="Origin"
          options={ORIGINS}
          value={query.origin}
          onChange={(origin) => commit({ ...query, q: text, origin })}
        />
        <span className="flex items-center gap-1.5">
          <Label id={sortId} className="text-xs font-normal text-muted-foreground">
            Sort
          </Label>
          <Select
            value={query.sort}
            onValueChange={(sort) => commit({ ...query, q: text, sort: sort as CatalogueSort })}
          >
            <SelectTrigger size="sm" aria-labelledby={sortId}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SORTS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </span>
        <SegmentedGroup
          label="View"
          options={VIEWS}
          value={query.view}
          onChange={(view) => commit({ ...query, q: text, view })}
        />
      </div>

      {/* Folded by default (#932): a catalogue has a hundred-odd tags, and laid out in
          full they pushed the first model a screen and a half down on a phone and put
          120 tab stops before it. A selected tag stays in view so it can be cleared. */}
      {tags.length > 0 && (
        <Collapsible open={tagsOpen} onOpenChange={setTagsOpen} className="flex flex-wrap items-center gap-1.5">
          <CollapsibleTrigger asChild>
            <Button variant="outline" size="xs" className="rounded-full" aria-controls={tagsId}>
              <ChevronRightIcon aria-hidden className={cn('transition-transform', tagsOpen && 'rotate-90')} />
              Tags <span className="sb-num text-muted-foreground">{tags.length}</span>
            </Button>
          </CollapsibleTrigger>
          {/* Not a CollapsibleContent: the selected tags stay in it while it is folded. */}
          <ToggleGroup
            id={tagsId}
            type="multiple"
            role="group"
            aria-label="Tags"
            size="sm"
            spacing={1}
            value={query.tags}
            onValueChange={(next) => {
              const changed = [...next.filter((tag) => !query.tags.includes(tag)), ...query.tags.filter((tag) => !next.includes(tag))]
              if (changed[0] !== undefined) toggleTag(changed[0])
            }}
            className="flex-wrap"
          >
            {shownTags.map(({ tag, count }) => (
              <ToggleGroupItem
                key={tag}
                value={tag}
                variant="outline"
                className="h-6 rounded-full px-2.5 text-xs font-normal data-[state=on]:border-primary/60 data-[state=on]:bg-primary/15"
              >
                {tag} <span className="sb-num text-muted-foreground">{count}</span>
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </Collapsible>
      )}

      <div className="flex items-center gap-3 text-xs text-muted-foreground">
        <p data-testid="result-count" className="sb-num">
          {shown} of {total}
        </p>
        {filtered && (
          <Button size="xs" variant="ghost" onClick={() => commit(clearFilters(query))}>
            <XIcon /> Clear filters
          </Button>
        )}
      </div>
    </div>
  )
}

/** A row of toggle buttons, one pressed: the origin filter and the view. */
function SegmentedGroup<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string
  options: Array<{ value: T; label: string }>
  value: T
  onChange: (value: T) => void
}) {
  return (
    <ToggleGroup
      type="single"
      aria-label={label}
      variant="outline"
      size="sm"
      spacing={0}
      value={value}
      // Radix answers '' for a click on the pressed one: one stays pressed.
      onValueChange={(next) => next && onChange(next as T)}
    >
      {options.map((option) => (
        <ToggleGroupItem key={option.value} value={option.value} className="px-3 text-xs">
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}
