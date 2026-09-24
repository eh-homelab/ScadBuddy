import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ProjectChoices, ProjectRequest, ProjectView } from '../api/types'
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
 * The control fetches its own list and owns the "remember" write, so the parent needs to
 * know nothing but the chosen id: it passes that as `project_id` on the run, and the
 * backend resolves the folder from it. Attaching the resulting queue entries and archives
 * is deliberately *not* here — neither id exists when a print starts (#89).
 */

/** The select's sentinel for "New project…"; every other value is an id or ''. */
const NEW = 'new'

/**
 * Bambuddy shows a project by its folder and its counts, and a native `<option>` has no
 * room for secondary text, so it goes in the label. The folder is named first because it
 * is the part that decides whether the send has anywhere to go.
 */
function optionLabel(project: ProjectView): string {
  const parts = [project.folder_name ?? 'no folder yet']
  if (project.archive_count > 0) parts.push(`${project.archive_count} archived`)
  if (project.queue_count > 0) parts.push(`${project.queue_count} queued`)
  return `${project.name} · ${parts.join(' · ')}`
}

interface Props {
  slug: string
  /** The project in view. The parent owns it because the run request carries it. */
  value: number | null
  onChange: (projectId: number | null) => void
  /**
   * The model's remembered project, reported once the list has loaded, so the parent can
   * seed `value` from it without ever handling a `ProjectChoices`.
   */
  onLoaded?: (projectId: number | null) => void
}

export function ProjectPicker({ slug, value, onChange, onLoaded }: Props) {
  const [choices, setChoices] = useState<ProjectChoices | null>(null)
  const [remembered, setRemembered] = useState(false)
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [colour, setColour] = useState('')

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * Held in a ref because it is a notification, not an input to the load: a parent that
   * passes an inline arrow would otherwise change `load`'s identity on every render and
   * re-fetch the list forever.
   */
  const report = useRef(onLoaded)
  useEffect(() => {
    report.current = onLoaded
  })

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const next = await api.getProjects(slug)
      setChoices(next)
      report.current?.(next.model_project_id ?? null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not list the projects.')
    } finally {
      setLoading(false)
    }
  }, [slug])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * The tick follows the selection: it means "the project in view *is* this model's
   * project", not "this model has one". Without that, opening the picker on a model that
   * already has a project and choosing another one for a single print would leave the box
   * ticked and silently re-point the model at it.
   */
  useEffect(() => {
    setRemembered(value !== null && value === (choices?.model_project_id ?? null))
  }, [value, choices])

  const projects = choices?.projects ?? []
  const current = projects.find((project) => project.id === value)

  async function remember(next: boolean) {
    setRemembered(next)
    const stored = choices?.model_project_id ?? null
    // Same rule as the pipeline default in `PrintPicker`: the stored value only moves on
    // a real change of intent — ticking the box on a project that is not already stored,
    // or unticking it on the one that is. Selecting a different project for one print
    // says nothing about what this model should be filed under, so it writes nothing.
    const target = next && stored !== value ? value : !next && stored === value ? null : undefined
    if (target === undefined) return
    try {
      await api.putModelProject(slug, target)
      setChoices((choice) => (choice ? { ...choice, model_project_id: target } : choice))
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not remember the project.')
      // The tick states what is stored, so it has to go back when the write did not land.
      setRemembered(!next)
    }
  }

  async function create() {
    const body: ProjectRequest = {
      name: name.trim(),
      description: description.trim() || null,
      colour: colour.trim() || null,
    }
    setSaving(true)
    setError(null)
    try {
      const created = await api.createProject(body)
      // The POST answers with the whole view, folder included, so re-listing would only
      // fetch back what is already in hand.
      setChoices((choice) =>
        choice ? { ...choice, projects: [created, ...(choice.projects ?? [])] } : choice,
      )
      setCreating(false)
      setName('')
      setDescription('')
      setColour('')
      onChange(created.id)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not create the project.')
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
    <div>
      <label htmlFor="print-project" className="block text-[13px]">
        Project
      </label>
      <select
        id="print-project"
        data-testid="project-select"
        value={creating ? NEW : value === null ? '' : String(value)}
        onChange={(event) => {
          if (event.target.value === NEW) {
            setCreating(true)
            return
          }
          setCreating(false)
          onChange(event.target.value === '' ? null : Number(event.target.value))
        }}
        className="sb-field mt-1.5 cursor-pointer"
      >
        <option value="">No project</option>
        {projects.map((project) => (
          <option key={project.id} value={project.id}>
            {optionLabel(project)}
          </option>
        ))}
        <option value={NEW} data-testid="new-project">
          New project&hellip;
        </option>
      </select>

      {!creating && current && current.folder_id === null && (
        <p className="mt-1.5 text-[12px] text-muted" data-testid="project-no-folder">
          {current.name} has no library folder yet &mdash; ScadBuddy creates one and links it
          the first time it sends here, because the folder is what Bambuddy&rsquo;s project
          page lists.
        </p>
      )}

      {creating && (
        <div className="mt-2 space-y-3 rounded-[6px] border border-line bg-surface-2 p-3">
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

      <label className="mt-3 flex cursor-pointer items-center gap-2 text-[13px]">
        <input
          type="checkbox"
          data-testid="remember-project"
          checked={remembered}
          disabled={value === null}
          onChange={(event) => void remember(event.target.checked)}
          className="accent-[var(--sb-accent)]"
        />
        Remember this project for this model
      </label>

      {error && (
        <p role="alert" className="mt-2 text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
