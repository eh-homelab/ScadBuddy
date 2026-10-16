import { ChevronsUpDownIcon, FolderIcon, FolderKanbanIcon, PlusIcon } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { DestinationSort, FolderView, ProjectRequest, ProjectView } from '../api/types'
import { timeAgo } from '../lib/format'
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
 * Chooses the Bambuddy project this print is filed under (#79), or, where the parent
 * offers them, a library folder of no project instead (#3651).
 *
 * A project here is *two* Bambuddy objects, and that is the whole reason this control
 * exists rather than a folder dropdown: the project row carries the timeline and the BOM,
 * but it is the **library folder linked to it** that makes Bambuddy's project page list
 * any files at all. ScadBuddy always pairs them — creating a project creates its folder,
 * and linking a project that has none creates one — so a project chosen here is somewhere
 * the 3MF can actually land. A folder of no project is a plain place in the library: the
 * file lands there and is filed under no project.
 *
 * #3651 — a searchable list (fuzzy: typos and letters in order match), each row labelled
 * Project or Folder and shown with its path, sorted like a file explorer: by when it was
 * last touched (printed, filed or chosen here, or written to in Bambuddy; the default),
 * modified, or by name. Settings sets the order it opens on; the list can re-sort.
 *
 * The control fetches its own list, unless the parent passes one from
 * {@link useProjectList} (#317: the Customize page's picker and the print dialog's share
 * one list rather than each fetching it), so the parent needs to know nothing but the
 * chosen id: it passes that as `project_id` (or `folder_id`) on the run, and the backend
 * resolves the folder from it. Attaching the resulting queue entries and archives
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

function folderDetail(folder: FolderView): string {
  const files = folder.file_count ?? 0
  return `${files} ${files === 1 ? 'file' : 'files'}`
}

function optionLabel(project: ProjectView, path: string): string {
  return `${path} · ${detail(project)}`
}

/** #3651 — a row of the list: a project, or a library folder of no project. */
type Row =
  | { kind: 'project'; id: number; path: string; project: ProjectView }
  | { kind: 'folder'; id: number; path: string; folder: FolderView }

const SORTS: { value: DestinationSort; label: string }[] = [
  { value: 'touched', label: 'Last touched' },
  { value: 'modified', label: 'Modified' },
  { value: 'name', label: 'Name' },
]
const isSort = (value: unknown): value is DestinationSort => SORTS.some((sort) => sort.value === value)

/**
 * #3651 — what this page chose since it loaded, by row, so a choice moves to the top of
 * "Last touched" at once. The server's `touched_at` catches up once something is filed
 * there (or the Customize page remembers the project).
 */
const chosenAt = new Map<string, number>()

function markChosen(kind: Row['kind'], id: number) {
  chosenAt.set(`${kind}:${id}`, Date.now())
}

function stamp(iso: string | null | undefined): number | null {
  if (!iso) return null
  const at = Date.parse(iso)
  return Number.isNaN(at) ? null : at
}

function rowStamp(row: Row, sort: DestinationSort): number | null {
  const item = row.kind === 'project' ? row.project : row.folder
  if (sort === 'modified') return stamp(item.modified_at)
  const local = chosenAt.get(`${row.kind}:${row.id}`) ?? null
  const server = stamp(item.touched_at)
  return local === null ? server : server === null ? local : Math.max(local, server)
}

/** Newest first, rows with no date after the rest, ties by path; or by path alone. */
function ordered(rows: Row[], sort: DestinationSort): Row[] {
  const byName = [...rows].sort((a, b) => a.path.localeCompare(b.path, undefined, { sensitivity: 'base' }))
  if (sort === 'name') return byName
  const at = (row: Row) => rowStamp(row, sort) ?? Number.NEGATIVE_INFINITY
  return byName.sort((a, b) => at(b) - at(a))
}

/** The Select's value for "no parent": Radix keeps '' for "nothing chosen". */
const NO_PARENT = 'none'

interface Props {
  /** The project in view. The parent owns it because the run request carries it. */
  value: number | null
  onChange: (projectId: number | null) => void
  /**
   * #3651 — offers the library folders of no project too. `value` is the folder chosen;
   * a choice calls `onChange` with its id (the parent clears its project), and choosing
   * a project or No project calls it with `null` first.
   */
  folder?: { value: number | null; onChange: (folderId: number | null) => void }
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
  folder,
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
  /**
   * #3651 — the list's popover, and how it is sorted: as Settings says until the list is
   * re-sorted here, which holds for as long as the picker is on the page.
   */
  const [open, setOpen] = useState(false)
  const [sortChosen, setSortChosen] = useState<DestinationSort | null>(null)
  const sort = sortChosen ?? choices?.sort ?? 'touched'

  const reportProject = useRef(onProject)
  useEffect(() => {
    reportProject.current = onProject
  })

  const reportCreating = useRef(onCreating)
  useEffect(() => {
    reportCreating.current = onCreating
  })

  const projects = useMemo(() => choices?.projects ?? [], [choices])
  const folders = useMemo(() => (folder ? (choices?.folders ?? []) : []), [choices, folder])
  const paths = useMemo(() => breadcrumbs(projects), [projects])
  const folderId = folder?.value ?? null
  const current = folderId === null ? projects.find((project) => project.id === value) : undefined
  const currentFolder = folderId === null ? undefined : folders.find((entry) => entry.id === folderId)
  const rows = useMemo<Row[]>(
    () => [
      ...projects.map((project): Row => ({
        kind: 'project',
        id: project.id,
        path: paths.get(project.id) ?? project.name,
        project,
      })),
      ...folders.map((entry): Row => ({ kind: 'folder', id: entry.id, path: entry.path, folder: entry })),
    ],
    [projects, folders, paths],
  )
  // Ordered as the popover opens, so a choice does not reshuffle the list under the pointer.
  const shown = useMemo(() => (open ? ordered(rows, sort) : []), [open, rows, sort])
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
  const missing =
    choices !== null && folderId === null && value !== null && current === undefined ? value : null
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
      markChosen('project', created.id)
      folder?.onChange(null)
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
    if (projectId !== null) markChosen('project', projectId)
    folder?.onChange(null)
    onChange(projectId)
  }

  function chooseFolder(chosen: number) {
    setOpen(false)
    setCreating(false)
    markChosen('folder', chosen)
    folder?.onChange(chosen)
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
    : folderId !== null
      ? currentFolder
        ? `Folder: ${currentFolder.path} · ${folderDetail(currentFolder)}`
        : `Folder ${folderId}`
      : current
        ? optionLabel(current, paths.get(current.id) ?? current.name)
        : value === null
          ? 'No project'
          : `Project ${value}`
  const dataValue = creating
    ? 'new'
    : folderId !== null
      ? `folder-${folderId}`
      : value === null
        ? ''
        : String(value)
  const noun = folder ? 'projects and folders' : 'projects'

  return (
    <div className={inline ? 'flex flex-wrap items-center gap-2' : 'grid gap-1.5'}>
      <Label htmlFor={id} className={inline ? 'text-xs font-normal text-muted-foreground' : undefined}>
        {folder ? 'Project or folder' : 'Project'}
      </Label>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            id={id}
            data-testid={testId}
            data-value={dataValue}
            variant="outline"
            size={inline ? 'sm' : 'default'}
            role="combobox"
            aria-expanded={open}
            disabled={disabled || saving}
            className={cn('justify-between gap-2 font-normal', inline ? 'w-auto max-w-64' : 'w-full')}
          >
            {folderId !== null ? <FolderIcon className="opacity-60" /> : current && <FolderKanbanIcon className="opacity-60" />}
            <span className="min-w-0 flex-1 truncate text-left">{triggerText}</span>
            <ChevronsUpDownIcon className="opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="w-[min(26rem,calc(100vw-2rem))] gap-0 p-0"
          data-testid={`${testId}-list`}
        >
          <Command filter={(_value, search, keywords) => fuzzyScore((keywords ?? []).join(' '), search)}>
            <CommandInput placeholder={`Search ${noun}…`} aria-label={`Search ${noun}`} />
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
                  if (isSort(next)) setSortChosen(next)
                }}
              >
                {SORTS.map((option) => (
                  <ToggleGroupItem key={option.value} value={option.value} className="px-2 text-xs">
                    {option.label}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            </div>
            <CommandList>
              <CommandEmpty>{folder ? 'No project or folder matches.' : 'No project matches.'}</CommandEmpty>
              <CommandGroup>
                <CommandItem
                  value="none"
                  keywords={['No project', 'none', 'inbox']}
                  data-testid="project-option-none"
                  data-checked={!creating && folderId === null && value === null}
                  onSelect={() => choose(null)}
                >
                  No project
                </CommandItem>
              </CommandGroup>
              <CommandGroup heading={folder ? 'Projects and folders' : 'Projects'}>
                {shown.map((row) => {
                  const when = sort === 'name' ? null : rowStamp(row, sort)
                  const checked =
                    !creating &&
                    (row.kind === 'folder' ? folderId === row.id : folderId === null && value === row.id)
                  return (
                    <CommandItem
                      key={`${row.kind}-${row.id}`}
                      value={`${row.kind}-${row.id}`}
                      keywords={
                        row.kind === 'project'
                          ? [row.path, row.project.folder_name ?? '', 'project']
                          : [row.path, 'folder']
                      }
                      data-testid={row.kind === 'project' ? `project-option-${row.id}` : `folder-option-${row.id}`}
                      data-checked={checked}
                      onSelect={() => (row.kind === 'project' ? choose(row.id) : chooseFolder(row.id))}
                    >
                      {row.kind === 'project' ? (
                        <FolderKanbanIcon aria-hidden className="text-primary" />
                      ) : (
                        <FolderIcon aria-hidden />
                      )}
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate">{row.path}</span>
                        <span className="truncate text-xs text-muted-foreground">
                          {row.kind === 'project'
                            ? `Project · ${detail(row.project)}`
                            : `Folder · ${folderDetail(row.folder)}`}
                        </span>
                      </span>
                      {when !== null && (
                        <span className="shrink-0 text-xs text-muted-foreground">
                          {timeAgo(new Date(when).toISOString())}
                        </span>
                      )}
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
