/**
 * #4703 — the workspace file picker's list, `GET /api/v1/ai/workspace-files`, as agent
 * `routes/workspacePicker.ts` serves it. The sessions are the ones tests add with
 * `seedPickerSession`; their files are the session files
 * mock's (`sessionFiles.ts`), so a pick downloads them there, at the sha256 listed.
 *
 * With `resource_type` and `resource_id`: the sessions that touched it, archived ones
 * left out, each listed even without a file the target takes. Without: every session,
 * archived ones too, only those with a file the target takes or a title `q` matches.
 * `accept` and `q` filter as the agent does.
 */
import { HttpResponse, http } from 'msw'
import type { PickerFile, PickerSession, ResourceRef } from '../../api/types'
import { sessionFilesOf } from './sessionFiles'

type Seeded = Omit<PickerSession, 'files' | 'other_files'> & { touched: ResourceRef[] }

const state = { sessions: [] as Seeded[], unavailable: new Map<string, string>() }

export function reset(): void {
  state.sessions = []
  state.unavailable.clear()
}

/** Adds a session the picker lists; `touched` are the resources its tool calls changed. */
export function seedPickerSession(
  session: Partial<Seeded> & { id: string; touched?: ResourceRef[] },
): void {
  state.sessions.push({
    title: 'Untitled session',
    status: 'idle',
    archived: false,
    updated_at: new Date().toISOString(),
    unavailable: null,
    touched: [],
    ...session,
  })
}

/** Lists a session's file as one this agent cannot read now, with the reason. */
export function markPickerFileUnavailable(sessionId: string, path: string, reason: string): void {
  state.unavailable.set(`${sessionId}/${path}`, reason)
}

/** As agent `routes/workspacePicker.ts` `accepts`. */
function accepts(accept: string[], path: string, mediaType: string): boolean {
  if (accept.length === 0) return true
  const name = path.toLowerCase()
  return accept.some((entry) =>
    entry.startsWith('.') ? name.endsWith(entry) : entry.endsWith('/*') ? mediaType.startsWith(entry.slice(0, -1)) : mediaType === entry,
  )
}

function touches(session: Seeded, ref: ResourceRef): boolean {
  return session.touched.some((t) => t.id === ref.id && (t.type === ref.type || ref.type === 'model'))
}

export const handlers = [
  http.get('/api/v1/ai/workspace-files', ({ request }) => {
    const params = new URL(request.url).searchParams
    const type = params.get('resource_type')
    const id = params.get('resource_id')
    if ((type === null) !== (id === null)) return HttpResponse.json({ detail: 'resource_type and resource_id go together' }, { status: 400 })
    const resource = type !== null && id !== null ? ({ type, id } as ResourceRef) : null
    const accept = (params.get('accept') ?? '')
      .split(',')
      .map((e) => e.trim().toLowerCase())
      .filter(Boolean)
    const q = (params.get('q') ?? '').trim().toLowerCase()
    const listed = state.sessions
      .filter((s) => (resource ? touches(s, resource) && !s.archived : true))
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    const sessions: PickerSession[] = []
    for (const { touched: _touched, ...s } of listed) {
      const all = sessionFilesOf(s.id)
      const titleMatch = q !== '' && s.title.toLowerCase().includes(q)
      const taken = all.filter((f) => accepts(accept, f.path, f.media_type))
      const shown = q === '' || titleMatch ? taken : taken.filter((f) => f.path.toLowerCase().includes(q))
      if (q !== '' && !titleMatch && shown.length === 0) continue
      if (!resource && shown.length === 0 && !titleMatch) continue
      const files: PickerFile[] = shown.map((f) => ({ ...f, unavailable: state.unavailable.get(`${s.id}/${f.path}`) ?? null }))
      sessions.push({ ...s, other_files: all.length - taken.length, files })
    }
    return HttpResponse.json({ sessions })
  }),
]

