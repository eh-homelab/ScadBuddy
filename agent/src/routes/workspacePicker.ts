import type { Context, Hono } from 'hono'
import { browserUser } from '../auth/browserSession.js'
import type { OriginPolicy } from '../http/origins.js'
import type { SessionManager, SessionRecord } from '../sessions/manager.js'
import { ID_MAX, LOOKUP_TYPES, type LookupType, type ResourceRef } from '../sessions/touched.js'
import type { SessionFiles } from '../workspace/store.js'
import { type RemoteAddress, uiReadProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'
import { fileView, type WorkspaceFileView } from './sessionFiles.js'
import { LIST_LIMIT_MAX } from './sessions.js'

// The UI's workspace file picker (#4703): wherever the UI takes a file, the user may
// pick one from an assistant session's workspace (#2329) instead of uploading one.
//
//   GET /api/v1/ai/workspace-files[?resource_type=&resource_id=&q=&accept=&limit=]
//       → {sessions: [{id, title, status, archived, updated_at, unavailable,
//                      other_files, files: [file + {unavailable}]}]}
//
// With `resource_type` and `resource_id`: the sessions whose tool calls touched that
// resource (as GET /api/v1/ai/sessions filters them, #931), archived ones left out,
// each listed even when it holds no file the target takes. Without them ("Advanced"):
// every session the browser user may see, archived ones too, but only those that
// hold a file the target takes (or whose title matches `q`). Newest first, at most
// `limit` sessions (100; at most 500). `accept` and `q` are matched in the session
// list's own query (SessionManager.list `accept`, `withFile` and `search`: titles, and
// the paths and media types in ai_session_files) before the limit, so an older session
// that holds the only file the target takes, or that matches `q`, is found.
//
// `accept` is the target's own file input `accept` (a comma list of `.ext`,
// `type/subtype` and `type/*`); only files it takes are listed, and `other_files`
// counts the rest. `q` matches a session's title or a file's path, ignoring case:
// a title match lists every file the target takes, a path match only the files
// whose path matches.
//
// `unavailable` says why a file (or every file of a session) cannot be read now:
// its bytes are a sealed blob this agent cannot open (workspace/store.ts
// `sealedUnreadable`). Such files are still listed, so the user sees why a file
// they expected cannot be picked.
//
// Access is the session routes' (routes/sessions.ts): a read that passes guard.ts
// `uiReadProblem`, over SessionManager.list as the browser user, so the picker never
// shows a file from a session the user cannot see. The pick itself is a download with
// `expected_sha256` (routes/sessionFiles.ts): refused once the file has changed.

export type PickerFile = WorkspaceFileView & { unavailable: string | null }

export type PickerSession = {
  id: string
  title: string
  status: SessionRecord['status']
  archived: boolean
  updated_at: string
  /** Why none of its files the target takes can be read now, when it has such files and none can. */
  unavailable: string | null
  /** Its files the target does not take. */
  other_files: number
  files: PickerFile[]
}

export type WorkspacePickerDeps = {
  sessions: SessionManager | undefined
  workspace: SessionFiles | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

export const PICKER_LIMIT_DEFAULT = 100
export const QUERY_MAX = 200
export const ACCEPT_MAX = 1000
/** One `accept` entry: `.ext`, `type/subtype` or `type/*`. */
const ACCEPT_ENTRY = /^(\.[a-z0-9][a-z0-9.+-]{0,31}|[a-z0-9][a-z0-9.+-]{0,63}\/(\*|[a-z0-9][a-z0-9.+-]{0,126}))$/

/** The entries of an `accept` list, lowercased, or undefined when one is not an entry. */
export function acceptList(raw: string): string[] | undefined {
  const entries = raw
    .split(',')
    .map((e) => e.trim().toLowerCase())
    .filter((e) => e !== '')
  return entries.every((e) => ACCEPT_ENTRY.test(e)) ? entries : undefined
}

/** Whether a file with this path and media type is one an `accept` list takes; every file without a list. */
export function accepts(accept: readonly string[] | undefined, path: string, mediaType: string): boolean {
  if (!accept || accept.length === 0) return true
  const name = path.toLowerCase()
  const type = mediaType.toLowerCase()
  return accept.some((entry) =>
    entry.startsWith('.') ? name.endsWith(entry) : entry.endsWith('/*') ? type.startsWith(entry.slice(0, -1)) : type === entry,
  )
}

function limitOf(raw: string | undefined): number | undefined {
  if (raw === undefined) return PICKER_LIMIT_DEFAULT
  if (!/^\d{1,4}$/.test(raw)) return undefined
  const n = Number(raw)
  return n >= 1 && n <= LIST_LIMIT_MAX ? n : undefined
}

export function registerWorkspacePickerRoutes(app: Hono, deps: WorkspacePickerDeps): void {
  app.get('/api/v1/ai/workspace-files', async (c: Context) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'workspace file reads')
    if (problem) return c.json({ detail: problem }, 403)
    if (!deps.sessions || !deps.workspace) return c.json({ detail: NO_DATABASE }, 503)
    if (!(await deps.ready())) return c.json({ detail: NOT_READY }, 503)

    const type = c.req.query('resource_type')
    const id = c.req.query('resource_id')
    if ((type === undefined) !== (id === undefined)) return c.json({ detail: 'resource_type and resource_id go together' }, 400)
    let resource: ResourceRef | undefined
    if (type !== undefined) {
      if (!(LOOKUP_TYPES as readonly string[]).includes(type)) {
        return c.json({ detail: `resource type must be one of ${LOOKUP_TYPES.join(', ')}` }, 400)
      }
      if (!id || id.length > ID_MAX) return c.json({ detail: `resource id must be 1 to ${ID_MAX} characters` }, 400)
      resource = { type: type as LookupType, id }
    }
    const q = (c.req.query('q') ?? '').trim().toLowerCase()
    if (q.length > QUERY_MAX) return c.json({ detail: `q must be at most ${QUERY_MAX} characters` }, 400)
    const rawAccept = c.req.query('accept')
    if (rawAccept !== undefined && rawAccept.length > ACCEPT_MAX) {
      return c.json({ detail: `accept must be at most ${ACCEPT_MAX} characters` }, 400)
    }
    const accept = rawAccept === undefined ? undefined : acceptList(rawAccept)
    if (rawAccept !== undefined && accept === undefined) {
      return c.json({ detail: 'accept: a comma list of `.ext`, `type/subtype` or `type/*`' }, 400)
    }
    const limit = limitOf(c.req.query('limit'))
    if (limit === undefined) return c.json({ detail: `limit must be an integer from 1 to ${LIST_LIMIT_MAX}` }, 400)

    const listed = await deps.sessions.list(browserUser(c), {
      ...(resource ? { resource } : { archived: 'include' as const }),
      ...(q ? { search: q } : {}),
      ...(accept?.length ? { accept } : {}),
      // Advanced lists only sessions with a file the target takes (or a title `q` matches, which `search` keeps).
      ...(!resource && !q ? { withFile: true } : {}),
      limit,
    })
    const byId = await deps.workspace.listSessions(listed.map((s) => s.id))
    const anySealed = [...byId.values()].some((files) => files.some((f) => f.sealed))
    const unreadable = anySealed ? await deps.workspace.sealedUnreadable() : undefined

    const out: PickerSession[] = []
    for (const s of listed) {
      const all = byId.get(s.id) ?? []
      const titleMatch = q !== '' && s.title.toLowerCase().includes(q)
      const taken = all.filter((f) => accepts(accept, f.path, f.mediaType))
      const shown = q === '' || titleMatch ? taken : taken.filter((f) => f.path.toLowerCase().includes(q))
      if (q !== '' && !titleMatch && shown.length === 0) continue
      if (!resource && shown.length === 0 && !titleMatch) continue
      const files = shown.map((f): PickerFile => ({
        ...fileView(f),
        unavailable: f.sealed && unreadable ? unreadable : null,
      }))
      out.push({
        id: s.id,
        title: s.title,
        status: s.status,
        archived: s.archivedAt !== null,
        updated_at: s.updatedAt,
        // Greyed when no file the target takes can be read now; its other files do not count.
        unavailable:
          taken.length > 0 && unreadable && taken.every((f) => f.sealed) ? `its workspace files of this kind cannot be read now: ${unreadable}` : null,
        other_files: all.length - taken.length,
        files,
      })
    }
    return c.json({ sessions: out })
  })
}

/** The UI's workspace file picker (#4703). `sessions` is declared in app.ts, `workspace` in routes/sessionFiles.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerWorkspacePickerRoutes(app, {
      sessions: deps.sessions,
      workspace: deps.workspace,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
