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
import { Button } from './ui/Button'

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

function chipClass(selected: boolean): string {
  return `rounded-full border px-2.5 py-0.5 text-[12px] transition-colors ${
    selected ? 'border-accent bg-accent/15 text-ink' : 'border-line text-muted hover:text-ink'
  }`
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

  return (
    <div className="mb-4 space-y-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <input
          ref={input}
          type="search"
          aria-label="Search models"
          placeholder="Search models  /"
          value={text}
          onChange={(event) => search(event.target.value)}
          className="sb-field h-8 min-w-48 flex-1"
        />
        <SegmentedGroup
          label="Origin"
          options={ORIGINS}
          value={query.origin}
          onChange={(origin) => commit({ ...query, q: text, origin })}
        />
        <label className="flex items-center gap-1.5 text-[12px] text-muted">
          Sort
          <select
            value={query.sort}
            onChange={(event) =>
              commit({ ...query, q: text, sort: event.target.value as CatalogueSort })
            }
            className="sb-field h-8 w-auto cursor-pointer"
          >
            {SORTS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
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
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            type="button"
            aria-expanded={tagsOpen}
            aria-controls={tagsId}
            onClick={() => setTagsOpen((open) => !open)}
            className="flex items-center gap-1 rounded-full border border-line px-2.5 py-0.5 text-[12px] text-muted transition-colors hover:text-ink"
          >
            <span
              aria-hidden="true"
              className={`inline-block transition-transform ${tagsOpen ? 'rotate-90' : ''}`}
            >
              ›
            </span>
            Tags <span className="sb-num text-faint">{tags.length}</span>
          </button>
          <div id={tagsId} role="group" aria-label="Tags" className="flex flex-wrap gap-1.5">
            {tags
              .filter(({ tag }) => tagsOpen || query.tags.includes(tag))
              .map(({ tag, count }) => {
                const selected = query.tags.includes(tag)
                return (
                  <button
                    key={tag}
                    type="button"
                    aria-pressed={selected}
                    onClick={() => toggleTag(tag)}
                    className={chipClass(selected)}
                  >
                    {tag} <span className="sb-num text-faint">{count}</span>
                  </button>
                )
              })}
          </div>
        </div>
      )}

      <div className="flex items-center gap-3 text-[12px] text-faint">
        <p data-testid="result-count" className="sb-num">
          {shown} of {total}
        </p>
        {filtered && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => commit(clearFilters(query))}
          >
            Clear filters
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
    <div
      role="group"
      aria-label={label}
      className="flex rounded-[6px] border border-line bg-surface p-0.5"
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          onClick={() => onChange(option.value)}
          className={`h-6 rounded-[4px] px-2.5 text-[12px] transition-colors ${
            value === option.value ? 'bg-surface-3 text-ink' : 'text-muted hover:text-ink'
          }`}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
