import { useEffect, useRef, useState, type ReactNode } from 'react'
import { SearchIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { CHIP, shownChips } from './filterChips'

/**
 * #4249 — the records pages' filters (spec 2026-10-18 §9): chip groups, selects and a
 * search field, extracted from the Activity filters so the orders and customers pages
 * share them. `ActivityFilters` composes the same parts around its facets.
 */

export function Count({ count }: { count: number | null | undefined }) {
  return count === null || count === undefined ? null : <span className="sb-num text-muted-foreground">{count}</span>
}

/** A labelled row of chips, with the "Show more" toggle when `shownChips` left some out. */
export function ChipGroup({
  label,
  children,
  hidden = 0,
  all = false,
  onAll,
}: {
  label: string
  children: ReactNode
  /** How many chips `shownChips` left out; a toggle shows them. */
  hidden?: number
  all?: boolean
  onAll?: (all: boolean) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span aria-hidden="true" className="text-xs text-muted-foreground">
        {label}
      </span>
      {children}
      {onAll && (all || hidden > 0) && (
        <Button
          variant="link"
          size="sm"
          className="h-auto px-1 text-xs"
          aria-expanded={all}
          aria-label={all ? `${label}: show fewer` : `${label}: show ${hidden} more`}
          onClick={() => onAll(!all)}
        >
          {all ? 'Show fewer' : `Show ${hidden} more`}
        </Button>
      )}
    </div>
  )
}

/** A text input that reports after a pause, and follows the URL when it changes elsewhere. */
export function DebouncedInput({
  value,
  onCommit,
  ...rest
}: { value: string; onCommit: (value: string) => void } & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange'>) {
  const [text, setText] = useState(value)
  const sent = useRef(value)
  const commit = useRef(onCommit)
  useEffect(() => {
    commit.current = onCommit
  })
  useEffect(() => {
    if (value !== sent.current) {
      sent.current = value
      setText(value)
    }
  }, [value])
  useEffect(() => {
    if (text === sent.current) return
    const timer = setTimeout(() => {
      sent.current = text
      commit.current(text)
    }, 250)
    return () => clearTimeout(timer)
  }, [text])
  return <Input {...rest} value={text} onChange={(e) => setText(e.target.value)} />
}

/** The search box with its icon. */
export function SearchField({ value, onChange, placeholder }: { value: string; onChange: (value: string) => void; placeholder: string }) {
  return (
    <div className="relative w-full max-w-xs">
      <SearchIcon aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
      <DebouncedInput type="search" className="pl-8" placeholder={placeholder} aria-label={placeholder} value={value} onCommit={onChange} />
    </div>
  )
}

export interface Chip {
  value: string
  label: ReactNode
  count?: number | null
  title?: string
}

export interface ChipGroupSpec {
  key: string
  label: string
  chips: Chip[]
  /** The selected values; one at most unless `multiple`. */
  selected: string[]
  multiple?: boolean
  onChange: (selected: string[]) => void
}

export interface SelectSpec {
  key: string
  label: string
  value: string
  options: { value: string; label: string }[]
  onChange: (value: string) => void
}

export interface FilterBarProps {
  /** The bar's accessible name. */
  label: string
  search?: { value: string; onChange: (value: string) => void; placeholder: string }
  selects?: SelectSpec[]
  chips?: ChipGroupSpec[]
  /** Whether anything is in force: shows "Clear filters", which calls `onClear`. */
  filtered?: boolean
  onClear?: () => void
  children?: ReactNode
}

/** A filter bar: the search and selects on one row, then a row of chips per group. */
export function FilterBar({ label, search, selects = [], chips = [], filtered = false, onClear, children }: FilterBarProps) {
  const [all, setAll] = useState<Record<string, boolean>>({})
  return (
    <div className="flex flex-col gap-2.5" aria-label={label} role="search">
      {(search || selects.length > 0 || filtered) && (
        <div className="flex flex-wrap items-center gap-2">
          {search && <SearchField {...search} />}
          {selects.map((select) => (
            <Select key={select.key} value={select.value} onValueChange={select.onChange}>
              <SelectTrigger aria-label={select.label}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent position="popper" align="start">
                {select.options.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ))}
          {filtered && onClear && (
            <Button variant="ghost" size="sm" onClick={onClear}>
              Clear filters
            </Button>
          )}
        </div>
      )}
      {chips.map((group) => {
        const shown = shownChips(group.chips, (c) => c.value, group.selected, all[group.key] === true)
        const items = shown.map((chip) => (
          <ToggleGroupItem key={chip.value} value={chip.value} className={CHIP} title={chip.title}>
            {chip.label}
            <Count count={chip.count} />
          </ToggleGroupItem>
        ))
        return (
          <ChipGroup
            key={group.key}
            label={group.label}
            hidden={group.chips.length - shown.length}
            all={all[group.key]}
            onAll={(next) => setAll((was) => ({ ...was, [group.key]: next }))}
          >
            {group.multiple ? (
              <ToggleGroup
                type="multiple"
                variant="outline"
                size="sm"
                spacing={1.5}
                aria-label={group.label}
                className="flex-wrap"
                value={group.selected}
                onValueChange={group.onChange}
              >
                {items}
              </ToggleGroup>
            ) : (
              <ToggleGroup
                type="single"
                variant="outline"
                size="sm"
                spacing={1.5}
                aria-label={group.label}
                className="flex-wrap"
                value={group.selected[0] ?? ''}
                onValueChange={(value) => group.onChange(value ? [value] : [])}
              >
                {items}
              </ToggleGroup>
            )}
          </ChipGroup>
        )
      })}
      {children}
    </div>
  )
}
