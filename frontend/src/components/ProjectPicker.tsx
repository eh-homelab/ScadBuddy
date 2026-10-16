import { ChevronsUpDownIcon, PlusIcon } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { ProjectRequest, ProjectView } from '../api/types'
import { fuzzyScore } from '../lib/fuzzy'
import { breadcrumbs, type ProjectList, useProjectList } from '../lib/projects'
import { Button } from '@/components/ui/button'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from '@/components/ui/command'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'
import { Spinner } from './ui/Spinner'

/**
 * Chooses the Bambuddy project this print is filed under (#79).
 *
 * A project here is *two* Bambuddy objects, and that is the whole reason this control
 * exists rather than a folder dropdown: the project row carries the timeline and the BOM,
 * but it is the **library folder linked to it** that makes Bambuddy's project page list
 * any files at all. ScadBuddy always pairs them — creating a project creates its folder,
 * and linking a project that has none creates one — so a project chosen here is somewhere
 * the 3MF can actually land.
 *
 * #3651 — a searchable list (fuzzy: typos and letters in order match), most recently
 * chosen first or by name, each project shown with its path and folder.
 *
 * The control fetches its own list, unless the parent passes one from
 * {@link useProjectList} (#317: the Customize page's picker and the print dialog's share
 * one list rather than each fetching it), so the parent needs to know nothing but the
 * chosen id: it passes that as `project_id` on the run, and the backend resolves the
 * folder from it. Attaching the resulting queue entries and archives
 * is deliberately *not* here — neither id exists when a print starts (#89).
 */

/**
 * Bambuddy shows a project by its folder and its counts. The folder is named first because
 * it is the part that decides whether the send has anywhere to go.
 */
function detail(project: ProjectView): string {
  const parts = [project.folder_name ?? 'no folder yet']
  if (project.archive_count > 0) parts.push(`${project.archive_count} archived`)
  if (project.queue_count > 0) parts.push(`${project.queue_count} queued`)
  return parts.join(' · ')
}

function optionLabel(project: ProjectView, path: string): string {
  return `${path} · ${detail(project)}`
}

/** #3651 — how the list is ordered: most recently chosen first (the default), or by name. */
type Sort = 'recent' | 'name'
const SORT_KEY = 'scadbuddy.project-picker.sort'
const RECENT_KEY = 'scadbuddy.project-picker.recent'
const RECENT_MAX = 20

function readStored<T>(key: string, fallback: T, valid: (value: unknown) => value is T): T {
  try {
    const raw = window.localStorage.getItem(key)
    if (raw === null) return fallback
    const value: unknown = JSON.parse(raw)
    return valid(value) ? value : fallback
  } catch {
    return fallback
  }
}

function writeStored(key: string, value: unknown) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // A private window or blocked storage: the order is a convenience, kept for this visit.
  }
}

const isSort = (value: unknown): value is Sort => value === 'recent' || value === 'name'
const isIds = (value: unknown): value is number[] =>
  Array.isArray(value) && value.every((id) => typeof id === 'number')

/** The projects this browser chose most recently, newest first. */
function recentIds(): number[] {
  return readStored(RECENT_KEY, [], isIds)
}

function touch(projectId: number) {
  writeStored(RECENT_KEY, [projectId, ...recentIds().filter((id) => id !== projectId)].slice(0, RECENT_MAX))
}

/**
 * Most recently touched first: this browser's choices, then the project the last send
 * went to (`last_project_id`), then the rest by name.
 */
function ordered(
  projects: ProjectView[],
  paths: Map<number, string>,
  sort: Sort,
  lastProjectId: number | null,
): ProjectView[] {
  const byName = [...projects].sort((a, b) =>
    (paths.get(a.id) ?? a.name).localeCompare(paths.get(b.id) ?? b.name, undefined, { sensitivity: 'base' }),
  )
  if (sort === 'name') return byName
  const rank = [...recentIds(), ...(lastProjectId === null ? [] : [lastProjectId])]
  const at = (id: number) => {
    const index = rank.indexOf(id)
    return index === -1 ? Number.POSITIVE_INFINITY : index
  }
  return byName.sort((a, b) => at(a.id) - at(b.id))
}

/** The Select's value for "no parent": Radix keeps '' for "nothing chosen". */
const NO_PARENT = 'none'

interface Props {
  /** The project in view. The parent owns it because the run request carries it. */
  value: number | null
  onChange: (projectId: number | null) => void
  /**
   * The project the last send went to, reported once the list has loaded, so the parent
   * can seed `value` from it without ever handling a `ProjectChoices`.
   */
  onLoaded?: (projectId: number | null) => void
  /**
   * #317 — a list the parent shares between pickers. Given, this picker fetches nothing
   * and `onLoaded` is the parent's business (it passed it to {@link useProjectList}).
   */
  list?: ProjectList
  /**
   * #317 — the chosen project's row, whenever it changes, so the parent can name it
   * ("Saved to Kids' room") without fetching the list a second time.
   */
  onProject?: (project: ProjectView | null) => void
  /** The select's id and test id, which differ when two pickers share a page (#317). */
  id?: string
  testId?: string
  /** The label beside the select rather than above it, for the Customize page's bar. */
  inline?: boolean
  /**
   * #665 — frozen while a request that already carries the chosen project is in flight
   * (Generate filing its file), so what is on screen matches where it was filed.
   */
  disabled?: boolean
  /**
   * #665 — whether a "Create project" is in flight, so the parent can hold Generate:
   * the create's completion moves `value`, which must not happen under a Generate.
   */
  onCreating?: (creating: boolean) => void
}

export function ProjectPicker({
  value,
  onChange,
  onLoaded,
  list,
  onProject,
  id = 'print-project',
  testId = 'project-select',
  inline = false,
  disabled = false,
  onCreating,
}: Props) {
  const own = useProjectList(onLoaded, list === undefined)
  const { choices, loading, error: listError, retry, rereadFor, add } = list ?? own
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [colour, setColour] = useState('')
  /** #930 — the project to nest the new one under; `null` (None) is the default. */
  const [parentId, setParentId] = useState<number | null>(null)
  /** The project in view when "New project…" was chosen, offered but never pre-selected. */
  const [suggestedParent, setSuggestedParent] = useState<number | null>(null)

  const [saving, setSaving] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const error = createError ?? listError
  /** #3651 — the list's popover, and how it is sorted (remembered in this browser). */
  const [open, setOpen] = useState(false)
  const [sort, setSortState] = useState<Sort>(() => readStored(SORT_KEY, 'recent', isSort))
  const setSort = (next: Sort) => {
    setSortState(next)
    writeStored(SORT_KEY, next)
  }

  const reportProject = useRef(onProject)
  useEffect(() => {
    reportProject.current = onProject
  })

  const reportCreating = useRef(onCreating)
  useEffect(() => {
    reportCreating.current = onCreating
  })

  const projects = useMemo(() => choices?.projects ?? [], [choices])
  const paths = useMemo(() => breadcrumbs(projects), [projects])
  const current = projects.find((project) => project.id === value)
  const lastProjectId = choices?.last_project_id ?? null
  // Ordered as the popover opens, so a choice does not reshuffle the list under the pointer.
  const shown = useMemo(
    () => (open ? ordered(projects, paths, sort, lastProjectId) : []),
    [open, projects, paths, sort, lastProjectId],
  )
  const suggested = projects.find((project) => project.id === suggestedParent)

  useEffect(() => {
    reportProject.current?.(current ?? null)
  }, [current])

  /**
   * #317 — the other picker on the page may have just created the project in view, which
   * this one's list predates. Re-read once per such id across every picker sharing the
   * list (`rereadFor`). The re-read only refreshes the list: `value` is the parent's, and
   * the remembered `last_project_id` may still be the old project.
   */
  const missing = choices !== null && value !== null && current === undefined ? value : null
  useEffect(() => {
    if (missing !== null) rereadFor(missing)
  }, [missing, rereadFor])

  async function create() {
    const body: ProjectRequest = {
      name: name.trim(),
      description: description.trim() || null,
      colour: colour.trim() || null,
      // Only when chosen: an unset parent is not sent at all.
      ...(parentId !== null && { parent_id: parentId }),
    }
    setSaving(true)
    // Tied to the request, not this component: the picker can unmount mid-create (the
    // dialog toggling to Simple), and the guard must hold until the request settles.
    reportCreating.current?.(true)
    setCreateError(null)
    try {
      const created = await api.createProject(body)
      add(created)
      setCreating(false)
      setName('')
      setDescription('')
      setColour('')
      setParentId(null)
      touch(created.id)
      onChange(created.id)
    } catch (cause) {
      setCreateError(cause instanceof ApiError ? cause.detail : 'Could not create the project.')
    } finally {
      reportCreating.current?.(false)
      setSaving(false)
    }
  }

  function choose(projectId: number | null) {
    setOpen(false)
    setCreating(false)
    if (projectId !== null) touch(projectId)
    onChange(projectId)
  }

  function startCreating() {
    setOpen(false)
    // A fresh form every time: a parent chosen in a cancelled one must not carry over
    // into this project's nesting.
    setParentId(null)
    setSuggestedParent(current?.id ?? null)
    setCreating(true)
  }

  if (loading) {
    return (
      <div role="status" className="flex items-center gap-2 text-[13px] text-muted-foreground">
        <span className="sr-only">Loading projects</span>
        <Skeleton className={cn('h-8', inline ? 'w-40' : 'w-full')} />
      </div>
    )
  }

  const triggerText = creating
    ? 'New project…'
    : current
      ? optionLabel(current, paths.get(current.id) ?? current.name)
      : value === null
        ? 'No project'
        : `Project ${value}`

  return (
    <div className={inline ? 'flex flex-wrap items-center gap-2' : 'grid gap-1.5'}>
      <Label htmlFor={id} className={inline ? 'text-xs font-normal text-muted-foreground' : undefined}>
        Project
      </Label>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id={id}
            data-testid={testId}
            data-value={creating ? 'new' : value === null ? '' : String(value)}
            variant="outline"
            size={inline ? 'sm' : 'default'}
            role="combobox"
            aria-expanded={open}
            disabled={disabled || saving}
            className={cn('justify-between gap-2 font-normal', inline ? 'w-auto max-w-64' : 'w-full')}
          >
            <span className="min-w-0 truncate">{triggerText}</span>
            <ChevronsUpDownIcon className="opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="w-[min(24rem,calc(100vw-2rem))] gap-0 p-0"
          data-testid={`${testId}-list`}
        >
          <Command filter={(_value, search, keywords) => fuzzyScore((keywords ?? []).join(' '), search)}>
            <CommandInput placeholder="Search projects…" aria-label="Search projects" />
            <div className="flex items-center justify-between gap-2 border-b px-2 py-1.5">
              <span className="text-xs text-muted-foreground" id={`${id}-sort`}>
                Sort
              </span>
              <ToggleGroup
                type="single"
                size="sm"
                variant="outline"
                value={sort}
                aria-labelledby={`${id}-sort`}
                onValueChange={(next) => {
                  if (isSort(next)) setSort(next)
                }}
              >
                <ToggleGroupItem value="recent" className="px-2 text-xs">
                  Recent
                </ToggleGroupItem>
                <ToggleGroupItem value="name" className="px-2 text-xs">
                  Name
                </ToggleGroupItem>
              </ToggleGroup>
            </div>
            <CommandList>
              <CommandEmpty>No project matches.</CommandEmpty>
              <CommandGroup>
                <CommandItem
                  value="none"
                  keywords={['No project', 'none']}
                  data-testid="project-option-none"
                  data-checked={!creating && value === null}
                  onSelect={() => choose(null)}
                >
                  No project
                </CommandItem>
              </CommandGroup>
              <CommandGroup heading="Projects">
                {shown.map((project) => {
                  const path = paths.get(project.id) ?? project.name
                  return (
                    <CommandItem
                      key={project.id}
                      value={`project-${project.id}`}
                      keywords={[path, project.folder_name ?? '']}
                      data-testid={`project-option-${project.id}`}
                      data-checked={!creating && value === project.id}
                      onSelect={() => choose(project.id)}
                    >
                      <span className="flex min-w-0 flex-col">
                        <span className="truncate">{path}</span>
                        <span className="truncate text-xs text-muted-foreground">{detail(project)}</span>
                      </span>
                    </CommandItem>
                  )
                })}
              </CommandGroup>
              <CommandSeparator />
              <CommandGroup>
                <CommandItem
                  value="new"
                  keywords={['New project', 'create']}
                  data-testid="new-project"
                  onSelect={startCreating}
                >
                  <PlusIcon />
                  New project&hellip;
                </CommandItem>
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>

      {!creating && current && current.folder_id === null && (
        <p className="basis-full text-xs text-muted-foreground" data-testid="project-no-folder">
          {current.name} has no library folder yet &mdash; ScadBuddy creates one and links it
          the first time it sends here, because the folder is what Bambuddy&rsquo;s project
          page lists.
        </p>
      )}

      {creating && (
        <div className="mt-1 grid basis-full gap-3 rounded-lg border bg-ui-muted/40 p-3">
          <div className="grid gap-1.5">
            <Label htmlFor="new-project-name">Name</Label>
            <Input
              id="new-project-name"
              data-testid="new-project-name"
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="new-project-parent">Parent project</Label>
            <Select
              value={parentId === null ? NO_PARENT : String(parentId)}
              onValueChange={(next) => setParentId(next === NO_PARENT ? null : Number(next))}
            >
              <SelectTrigger
                id="new-project-parent"
                data-testid="new-project-parent"
                data-value={parentId === null ? '' : String(parentId)}
                className="w-full"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_PARENT}>None</SelectItem>
                {projects.map((project) => (
                  <SelectItem key={project.id} value={String(project.id)}>
                    {paths.get(project.id) ?? project.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {suggested && parentId !== suggested.id && (
              <Button
                variant="link"
                size="sm"
                data-testid="suggested-parent"
                onClick={() => setParentId(suggested.id)}
                className="h-auto justify-start p-0 text-xs"
              >
                Put it under {paths.get(suggested.id) ?? suggested.name}
              </Button>
            )}
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="new-project-description">Description</Label>
            <Input
              id="new-project-description"
              type="text"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>

          <div className="grid gap-1.5">
            <Label htmlFor="new-project-colour">Colour</Label>
            {/* Text, not `type="color"`: a colour input always carries a value — #000000
                until it is touched — which would put a colour nobody chose on Bambuddy's
                project page. Blank means "send none". */}
            <Input
              id="new-project-colour"
              type="text"
              value={colour}
              placeholder="#ef4444"
              onChange={(event) => setColour(event.target.value)}
            />
          </div>

          <p className="text-xs text-muted-foreground">
            ScadBuddy also creates a <span className="text-foreground">library folder</span> of the
            same name and links it to the project. That is what makes Bambuddy&rsquo;s
            project page show the files &mdash; the project row on its own stays empty.
          </p>

          <div className="flex items-center gap-2">
            <Button
              size="sm"
              onClick={() => void create()}
              disabled={name.trim() === '' || saving || disabled}
              aria-busy={saving}
              data-testid="create-project"
              {...USER_ONLY}
            >
              {saving && <Spinner />}
              Create project
            </Button>
            <Button size="sm" variant="outline" onClick={() => setCreating(false)} disabled={saving}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="basis-full text-[13px] text-destructive">
          {error}
          {/* #1045 — nothing else reads a failed list again, short of a page reload. */}
          {createError === null && (
            <>
              {' '}
              <Button size="sm" variant="outline" onClick={retry} disabled={loading}>
                Retry
              </Button>
            </>
          )}
        </p>
      )}
    </div>
  )
}
