import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ProjectChoices, ProjectView } from '../api/types'

/** The project list, fetched once and shared by every picker given it (#317). */
export interface ProjectList {
  choices: ProjectChoices | null
  loading: boolean
  error: string | null
  /**
   * Re-reads the list. It never reports `last_project_id`: the value already belongs to
   * the parent, and the remembered id may predate a choice whose `PUT` has not landed.
   */
  reload: () => Promise<void>
  /** A project just created, put at the head of the list without re-reading it. */
  add: (project: ProjectView) => void
}

/**
 * Fetches the project list once and reports the last project it went to through
 * `onLoaded`, so the parent can seed its value. `enabled` false fetches nothing, for a
 * picker handed a list by its parent.
 */
export function useProjectList(
  onLoaded?: (projectId: number | null) => void,
  enabled = true,
): ProjectList {
  const [choices, setChoices] = useState<ProjectChoices | null>(null)
  const [loading, setLoading] = useState(enabled)
  const [error, setError] = useState<string | null>(null)

  /**
   * Held in a ref because it is a notification, not an input to the load: a parent that
   * passes an inline arrow would otherwise change `fetchList`'s identity on every render
   * and re-fetch the list forever.
   */
  const report = useRef(onLoaded)
  useEffect(() => {
    report.current = onLoaded
  })

  const fetchList = useCallback(async (seed: boolean) => {
    if (seed) setLoading(true)
    setError(null)
    try {
      const next = await api.getProjects()
      setChoices(next)
      if (seed) report.current?.(next.last_project_id ?? null)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not list the projects.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (enabled) void fetchList(true)
  }, [enabled, fetchList])

  const reload = useCallback(() => fetchList(false), [fetchList])
  const add = useCallback((project: ProjectView) => {
    // The POST answers with the whole view, folder included, so re-listing would only
    // fetch back what is already in hand.
    setChoices((choice) =>
      choice ? { ...choice, projects: [project, ...(choice.projects ?? [])] } : choice,
    )
  }, [])

  return useMemo(
    () => ({ choices, loading, error, reload, add }),
    [choices, loading, error, reload, add],
  )
}
