import { useEffect, useRef, useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { ProjectRequest, ProjectView } from '../api/types'
import { type ProjectList, useProjectList } from '../lib/projects'
import { Button } from './ui/Button'
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
 * The control fetches its own list, unless the parent passes one from
 * {@link useProjectList} (#317: the Customize page's picker and the print dialog's share
 * one list rather than each fetching it), so the parent needs to know nothing but the
 * chosen id: it passes that as `project_id` on the run, and the backend resolves the
 * folder from it. Attaching the resulting queue entries and archives
 * is deliberately *not* here — neither id exists when a print starts (#89).
 */

/** The select's sentinel for "New project…"; every other value is an id or ''. */
const NEW = 'new'

/**
 * Bambuddy shows a project by its folder and its counts, and a native `<option>` has no
 * room for secondary text, so it goes in the label. The folder is named first because it
 * is the part that decides whether the send has anywhere to go.
 */
function optionLabel(project: ProjectView, projects: ProjectView[]): string {
  const parts = [project.folder_name ?? 'no folder yet']
  if (project.archive_count > 0) parts.push(`${project.archive_count} archived`)
  if (project.queue_count > 0) parts.push(`${project.queue_count} queued`)
  return `${breadcrumb(project, projects)} · ${parts.join(' · ')}`
}

/**
 * #930 — a nested project named by its path, `Parent › Child`, since a native `<option>`
 * cannot indent. A parent missing from the list (or a cycle) ends the path there.
 */
function breadcrumb(project: ProjectView, projects: ProjectView[]): string {
  const names = [project.name]
  const seen = new Set([project.id])
  let parentId = project.parent_id ?? null
  while (parentId !== null && !seen.has(parentId)) {
    seen.add(parentId)
    const parent = projects.find((row) => row.id === parentId)
    if (!parent) break
    names.unshift(parent.name)
    parentId = parent.parent_id ?? null
  }
  return names.join(' › ')
}

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
}: Props) {
  const own = useProjectList(onLoaded, list === undefined)
  const { choices, loading, error: listError, rereadFor, add } = list ?? own
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

  const reportProject = useRef(onProject)
  useEffect(() => {
    reportProject.current = onProject
  })

  const projects = choices?.projects ?? []
  const current = projects.find((project) => project.id === value)
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
    setCreateError(null)
    try {
      const created = await api.createProject(body)
      add(created)
      setCreating(false)
      setName('')
      setDescription('')
      setColour('')
      setParentId(null)
      onChange(created.id)
    } catch (cause) {
      setCreateError(cause instanceof ApiError ? cause.detail : 'Could not create the project.')
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return (
      <p className="flex items-center gap-2 text-[13px] text-muted">
        <Spinner /> Loading projects
      </p>
    )
  }

  return (
    <div className={inline ? 'flex flex-wrap items-center gap-2' : undefined}>
      <label htmlFor={id} className={inline ? 'text-[12px] text-muted' : 'block text-[13px]'}>
        Project
      </label>
      <select
        id={id}
        data-testid={testId}
        value={creating ? NEW : value === null ? '' : String(value)}
        onChange={(event) => {
          if (event.target.value === NEW) {
            setSuggestedParent(current?.id ?? null)
            setCreating(true)
            return
          }
          setCreating(false)
          onChange(event.target.value === '' ? null : Number(event.target.value))
        }}
        className={inline ? 'sb-field w-auto max-w-56 cursor-pointer py-1 text-[12px]' : 'sb-field mt-1.5 cursor-pointer'}
      >
        <option value="">No project</option>
        {projects.map((project) => (
          <option key={project.id} value={project.id}>
            {optionLabel(project, projects)}
          </option>
        ))}
        <option value={NEW} data-testid="new-project">
          New project&hellip;
        </option>
      </select>

      {!creating && current && current.folder_id === null && (
        <p className="mt-1.5 basis-full text-[12px] text-muted" data-testid="project-no-folder">
          {current.name} has no library folder yet &mdash; ScadBuddy creates one and links it
          the first time it sends here, because the folder is what Bambuddy&rsquo;s project
          page lists.
        </p>
      )}

      {creating && (
        <div className="mt-2 basis-full space-y-3 rounded-[6px] border border-line bg-surface-2 p-3">
          <div>
            <label htmlFor="new-project-name" className="block text-[13px]">
              Name
            </label>
            <input
              id="new-project-name"
              data-testid="new-project-name"
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="sb-field mt-1.5"
            />
          </div>

          <div>
            <label htmlFor="new-project-parent" className="block text-[13px]">
              Parent project
            </label>
            <select
              id="new-project-parent"
              data-testid="new-project-parent"
              value={parentId === null ? '' : String(parentId)}
              onChange={(event) =>
                setParentId(event.target.value === '' ? null : Number(event.target.value))
              }
              className="sb-field mt-1.5 cursor-pointer"
            >
              <option value="">None</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {breadcrumb(project, projects)}
                </option>
              ))}
            </select>
            {suggested && parentId !== suggested.id && (
              <button
                type="button"
                data-testid="suggested-parent"
                onClick={() => setParentId(suggested.id)}
                className="mt-1 text-[12px] text-accent hover:underline"
              >
                Put it under {breadcrumb(suggested, projects)}
              </button>
            )}
          </div>

          <div>
            <label htmlFor="new-project-description" className="block text-[13px]">
              Description
            </label>
            <input
              id="new-project-description"
              type="text"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              className="sb-field mt-1.5"
            />
          </div>

          <div>
            <label htmlFor="new-project-colour" className="block text-[13px]">
              Colour
            </label>
            {/* Text, not `type="color"`: a colour input always carries a value — #000000
                until it is touched — which would put a colour nobody chose on Bambuddy's
                project page. Blank means "send none". */}
            <input
              id="new-project-colour"
              type="text"
              value={colour}
              placeholder="#ef4444"
              onChange={(event) => setColour(event.target.value)}
              className="sb-field mt-1.5"
            />
          </div>

          <p className="text-[12px] text-muted">
            ScadBuddy also creates a <span className="text-ink">library folder</span> of the
            same name and links it to the project. That is what makes Bambuddy&rsquo;s
            project page show the files &mdash; the project row on its own stays empty.
          </p>

          <div className="flex items-center gap-2">
            <Button
              variant="primary"
              size="sm"
              onClick={() => void create()}
              disabled={name.trim() === '' || saving}
              aria-busy={saving}
              data-testid="create-project"
              {...USER_ONLY}
            >
              {saving && <Spinner />}
              Create project
            </Button>
            <Button size="sm" onClick={() => setCreating(false)} disabled={saving}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="mt-2 basis-full text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
