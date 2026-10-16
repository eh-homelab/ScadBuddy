import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { LibraryEntry, LibraryListing, ModelSummary, Output } from '../api/types'
import { needsBackfill } from '../lib/arrange'
import { formatBbox, shortId, timeAgo } from '../lib/format'
import { useDisplayUnit } from '../lib/units'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'

type Props<T> = {
  /** Already in the arrange: shown ticked and not offered again. */
  chosen: (item: T) => boolean
  onAdd: (items: T[]) => void
  onCancel: () => void
}

/** A read in flight: a placeholder bar, and what is being read in words. */
function Reading({ children }: { children: string }) {
  return (
    <p role="status" className="flex items-center gap-2 text-[12px] text-muted-foreground">
      <Skeleton aria-hidden className="h-3 w-16" />
      {children}
    </p>
  )
}

/** The folder Select's value for the top level: Radix Select takes no empty value. */
const TOP = 'top'

function failure(cause: unknown, fallback: string): string {
  return cause instanceof ApiError ? cause.detail : fallback
}

/** One tick per item, and Add for the ticked ones. */
function PickList<T>({
  items,
  label,
  keyOf,
  chosen,
  onAdd,
  onCancel,
  what,
}: Props<T> & {
  items: T[]
  label: (item: T) => string
  keyOf: (item: T) => string
  what: string
}) {
  const [ticked, setTicked] = useState<string[]>([])
  const picked = items.filter((item) => ticked.includes(keyOf(item)))
  return (
    <>
      {items.length === 0 ? (
        <p className="text-[12px] text-subtle">No {what} here.</p>
      ) : (
        <ScrollArea className="rounded-lg border [&_[data-slot=scroll-area-viewport]]:max-h-48">
          <ul className="flex flex-col gap-0.5 p-1">
            {items.map((item) => {
              const key = keyOf(item)
              const already = chosen(item)
              return (
                <li key={key} className="text-[13px]">
                  <Label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 font-normal hover:bg-muted has-disabled:cursor-default has-disabled:hover:bg-transparent">
                    <Checkbox
                      checked={already || ticked.includes(key)}
                      disabled={already}
                      onCheckedChange={() =>
                        setTicked((now) => (now.includes(key) ? now.filter((k) => k !== key) : [...now, key]))
                      }
                    />
                    <span className="min-w-0 truncate">{label(item)}</span>
                  </Label>
                </li>
              )
            })}
          </ul>
        </ScrollArea>
      )}
      <div className="flex justify-end gap-2">
        <Button type="button" size="sm" variant="outline" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="button" size="sm" disabled={picked.length === 0} onClick={() => onAdd(picked)}>
          Add ({picked.length})
        </Button>
      </div>
    </>
  )
}

/** #1864 — Arrange's "Add files": printable files of one library folder at a time. */
export function AddFiles(props: Props<LibraryEntry>) {
  const [folderId, setFolderId] = useState<number | null>(null)
  const [listing, setListing] = useState<LibraryListing | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setListing(null)
    setError(null)
    api
      .listLibrary({ folderId, all: false })
      .then((next) => live && setListing(next))
      .catch((cause: unknown) => live && setError(failure(cause, 'Could not read the Bambuddy library.')))
    return () => {
      live = false
    }
  }, [folderId])
  return (
    <Card size="sm" role="group" aria-labelledby="arrange-add-files">
      <CardHeader>
        <CardTitle id="arrange-add-files" className="text-[13px]">
          Add files from the library
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
      <Select
        value={folderId === null ? TOP : String(folderId)}
        onValueChange={(next) => setFolderId(next === TOP ? null : Number(next))}
      >
        <SelectTrigger aria-label="Library folder" className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={TOP}>Top level</SelectItem>
          {(listing?.folders ?? []).map((folder) => (
            <SelectItem key={folder.id} value={String(folder.id)}>
              <span style={{ paddingLeft: `${(folder.depth ?? 0) * 0.75}rem` }}>{folder.name}</span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {error && (
        <p role="alert" className="text-[12px] text-destructive">
          {error}
        </p>
      )}
      {!listing && !error && <Reading>Reading the library</Reading>}
      {listing && (
        <PickList
          {...props}
          key={folderId ?? 'top'}
          items={(listing.files ?? []).filter((file) => file.printable)}
          keyOf={(file) => String(file.id)}
          label={(file) => file.filename}
          what="printable files"
        />
      )}
      </CardContent>
    </Card>
  )
}

/** #2285 — models by name, one alphabetical run whatever case a name starts with. */
function byName(models: ModelSummary[]): ModelSummary[] {
  const name = (model: ModelSummary) => model.name ?? model.slug
  return [...models].sort((a, b) => name(a).localeCompare(name(b), undefined, { sensitivity: 'base' }))
}

/** #1864 — Arrange's "Add from a model": a template, then any of its saved outputs. */
export function AddFromModel(props: Props<Output>) {
  const [models, setModels] = useState<ModelSummary[] | null>(null)
  const [slug, setSlug] = useState('')
  /** #2285 — narrows the model list, which runs to ~100 entries. */
  const [search, setSearch] = useState('')
  const unit = useDisplayUnit()
  const wanted = search.trim().toLowerCase()
  // Sorted once per list, not on every keystroke in the search (#3556).
  const sorted = useMemo(() => byName(models ?? []), [models])
  const shown = sorted.filter(
    (model) =>
      !wanted ||
      // A model is found by its slug as well as its name: the slug is what a URL shows.
      [model.name ?? '', model.slug].some((text) => text.toLowerCase().includes(wanted)) ||
      model.slug === slug,
  )
  const [outputs, setOutputs] = useState<Output[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    api
      .listModels()
      .then((next) => live && setModels(next))
      .catch((cause: unknown) => live && setError(failure(cause, 'Could not read the models.')))
    return () => {
      live = false
    }
  }, [])
  useEffect(() => {
    if (!slug) return
    let live = true
    setOutputs(null)
    setError(null)
    api
      .listOutputs(slug)
      .then((next) => live && setOutputs(next))
      .catch((cause: unknown) => live && setError(failure(cause, 'Could not read the outputs.')))
    return () => {
      live = false
    }
  }, [slug])
  return (
    <Card size="sm" role="group" aria-labelledby="arrange-add-model">
      <CardHeader>
        <CardTitle id="arrange-add-model" className="text-[13px]">
          Add outputs of a model
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
      <Input
        type="search"
        aria-label="Find a model"
        placeholder="Find a model"
        value={search}
        onChange={(event) => setSearch(event.target.value)}
      />
      <Select value={slug} onValueChange={setSlug} disabled={!models}>
        <SelectTrigger aria-label="Model" className="w-full">
          <SelectValue
            placeholder={models ? (shown.length > 0 ? 'Choose a model' : 'No model matches') : 'Reading the models…'}
          />
        </SelectTrigger>
        <SelectContent>
          {shown.map((model) => (
            <SelectItem key={model.slug} value={model.slug}>
              {model.name ?? model.slug}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {error && (
        <p role="alert" className="text-[12px] text-destructive">
          {error}
        </p>
      )}
      {slug && !outputs && !error && <Reading>Reading its outputs</Reading>}
      {slug && outputs && (
        <PickList
          {...props}
          key={slug}
          items={outputs}
          keyOf={(output) => output.id}
          // #2285 — when and how big, so two outputs of one name can be told apart.
          label={(output) =>
            [
              output.name ?? shortId(output.id),
              timeAgo(output.created_at),
              ...(output.bbox_mm ? [formatBbox(output.bbox_mm, unit)] : []),
              ...(needsBackfill(output) ? ['needs a re-render'] : []),
            ].join(' · ')
          }
          what="saved outputs"
        />
      )}
      {!slug && (
        <div className="flex justify-end">
          <Button type="button" size="sm" variant="outline" onClick={props.onCancel}>
            Cancel
          </Button>
        </div>
      )}
      </CardContent>
    </Card>
  )
}
