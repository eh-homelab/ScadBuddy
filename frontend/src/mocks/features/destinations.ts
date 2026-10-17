import { HttpResponse, http } from 'msw'
import type { DefaultDestination } from '../../api/types'
import { mockDestinationState } from '../handlers'
import { mockLibraryFileFolder } from './library'

/**
 * #3651 — the project some content opens on, as the backend decides it
 * (`bambuddy/destinations.py`): the one project all of it is in already, else the
 * Settings default (`last_project_id`, #3188), else none. A project no longer listed is
 * never a default.
 */

const base = '/api/v1'

export const handlers = [
  http.get(`${base}/print/projects/default`, ({ request }) => {
    const { projects, outputs, lastProjectId, inboxId } = mockDestinationState()
    // The mocked inbox (2) doubles as Reagan Keychain's folder, which a real one never is.
    const ownerOf = (folderId: number | null | undefined) =>
      folderId == null || folderId === inboxId
        ? undefined
        : projects.find((project) => project.folder_id === folderId)?.id
    const search = new URL(request.url).searchParams
    const homes: Set<number>[] = [
      ...search.getAll('output_id').map((id) => {
        const output = outputs.find((o) => o.id === id)
        const home = new Set<number>()
        for (const copy of output?.library_files ?? []) {
          const owner = ownerOf(copy.folder_id)
          if (owner !== undefined) home.add(owner)
        }
        if (output?.project_id != null) home.add(output.project_id)
        return home
      }),
      ...search.getAll('library_file_id').map((id) => {
        const owner = ownerOf(mockLibraryFileFolder(Number(id)))
        return new Set(owner === undefined ? [] : [owner])
      }),
    ]
    const known = (id: number | null): id is number => id !== null && projects.some((project) => project.id === id)
    const shared = homes.length > 0 && homes.every((home) => home.size === 1) ? [...homes[0]!][0]! : null
    const together = shared !== null && homes.every((home) => home.has(shared))
    const answer: DefaultDestination =
      together && known(shared)
        ? { project_id: shared, reason: 'content' }
        : known(lastProjectId)
          ? { project_id: lastProjectId, reason: 'settings' }
          : { project_id: null, reason: 'none' }
    return HttpResponse.json(answer)
  }),
]
