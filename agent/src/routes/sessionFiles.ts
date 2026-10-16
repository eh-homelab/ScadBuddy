import type { Context, Hono, MiddlewareHandler } from 'hono'
import { bodyLimit } from 'hono/body-limit'
import type { TransactionSql } from 'postgres'
import type { AuditOutcome, AuditRepo } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { archivedError, type SessionManager, SessionError, type SessionRecord } from '../sessions/manager.js'
import { event, type ServerEvent, sameOwner } from '../sessions/protocol.js'
import {
  type SessionFiles,
  type WorkspaceFileInfo,
  WorkspaceError,
  type WorkspaceRef,
  workspacePath,
} from '../workspace/store.js'
import { BROWSER_USER } from './approvals.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// The assistant panel's Files (#2329, part 3): a session's workspace
// (workspace/store.ts `SessionFiles`, `ai_session_files` under `session:<id>`),
// over plain HTTP for the browser user.
//
//   GET    /api/v1/ai/sessions/:id/files          { files, usage: {files, bytes},
//                                                   limits: {file_max_bytes, workspace_max_bytes} }
//   GET    /api/v1/ai/sessions/:id/files/<path>   the bytes, always as an attachment
//   POST   /api/v1/ai/sessions/:id/files          multipart: `file`, `path`, and
//                                                   `expected_sha256` to replace a file
//                                                   → 201 {file} (200 when it replaced one);
//                                                   409 when `path` exists and no (or a stale)
//                                                   `expected_sha256` was sent; 413 past a cap
//   DELETE /api/v1/ai/sessions/:id/files/<path>[?expected_sha256=] → 204; 404 when there is none
//
// Access is the session routes' (routes/sessions.ts): reads pass guard.ts
// `uiReadProblem` and need a session the browser user may see; writes pass
// `uiRequestProblem` (the one origin allowlist, http/origins.ts), and only the
// session's owner may make them (403), never on an archived session (409). A
// write appends a `workspace.file` event to the session's log, so the panel's
// Files list reads again and a reader of the log sees what arrived, and is a
// `resource` audit row. The paths are checked by `workspacePath` (and again by
// the table); nothing here touches a file on the pod.
//
// A download is never shown inline: `Content-Disposition: attachment`, nosniff,
// a sandboxing CSP, and the stored media type except for the ones a browser
// would run (HTML, SVG, XML, scripts), which go as application/octet-stream.

export type SessionFileRouteDeps = {
  sessions: SessionManager | undefined
  workspace: SessionFiles | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  audit?: AuditRepo | undefined
}

declare module '../app.js' {
  interface AppDeps {
    /**
     * The assistant's workspace (#2329, workspace/store.ts): the panel's Files
     * routes (routes/sessionFiles.ts) read and write it. Undefined without a database.
     */
    workspace?: SessionFiles | undefined
  }
}

export type WorkspaceFileView = {
  path: string
  media_type: string
  size: number
  sha256: string
  source: WorkspaceFileInfo['source']
  created_at: string
  updated_at: string
}

export function fileView(info: WorkspaceFileInfo): WorkspaceFileView {
  return {
    path: info.path,
    media_type: info.mediaType,
    size: info.size,
    sha256: info.sha256,
    source: info.source,
    created_at: info.createdAt.toISOString(),
    updated_at: info.updatedAt.toISOString(),
  }
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

/** What a multipart body may carry besides the file: its boundaries and the other fields. */
export const MULTIPART_SLACK = 64 * 1024

const SHA256 = /^[0-9a-f]{64}$/
/** A bare `type/subtype`, lowercased; anything else is not stored or sent as a type. */
const MEDIA_TYPE = /^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$/
/** Types a browser renders or runs: downloaded as bytes, never as themselves. */
const ACTIVE_TYPE = /^(text\/html|application\/xhtml\+xml|image\/svg\+xml|text\/xml|application\/xml|[a-z]+\/[a-z0-9.+-]*\+xml|text\/javascript|application\/(x-)?javascript|application\/ecmascript|text\/ecmascript)$/

/** `type` as a bare media type, or undefined when it is not one. */
export function cleanMediaType(type: string | undefined): string | undefined {
  const bare = type?.split(';')[0]?.trim().toLowerCase()
  return bare && MEDIA_TYPE.test(bare) ? bare : undefined
}

/** The Content-Type a stored file is downloaded with. */
export function downloadType(stored: string): string {
  const bare = cleanMediaType(stored)
  return bare === undefined || ACTIVE_TYPE.test(bare) ? 'application/octet-stream' : bare
}

const STATUS_OF: Record<WorkspaceError['code'], 400 | 404 | 409 | 413> = {
  invalid_path: 400,
  not_found: 404,
  conflict: 409,
  too_large: 413,
  quota: 413,
}

const BASE = '/api/v1/ai/sessions'

/** The `<path>` after `/files/` in the request's own (still encoded) path, decoded; undefined when it does not decode. */
function filePathOf(c: Context): string | undefined {
  const pathname = new URL(c.req.url).pathname
  const at = pathname.indexOf('/files/')
  if (at < 0) return undefined
  try {
    return pathname
      .slice(at + '/files/'.length)
      .split('/')
      .map((segment) => decodeURIComponent(segment))
      .join('/')
  } catch {
    return undefined
  }
}

export function registerSessionFileRoutes(app: Hono, deps: SessionFileRouteDeps): void {
  type Ready = { sessions: SessionManager; files: SessionFiles }

  async function stores(): Promise<Ready | string> {
    if (!deps.sessions || !deps.workspace) return NO_DATABASE
    return (await deps.ready()) ? { sessions: deps.sessions, files: deps.workspace } : NOT_READY
  }

  /**
   * Guard, then the stores, then `fn`; SessionErrors and WorkspaceErrors become their status.
   * A write names its audit `action`: a write refused for the session's owner or its archive is audited (#2400).
   */
  function route(
    kind: 'read' | 'write',
    fn: (c: Context, ready: Ready, ws: WorkspaceRef, sessionId: string) => Promise<Response>,
    action?: string,
  ) {
    return async (c: Context) => {
      const problem =
        kind === 'read'
          ? uiReadProblem(c, deps.origins, deps.remoteAddress, 'session file reads')
          : uiRequestProblem(c, deps.origins, deps.remoteAddress, 'session file changes')
      if (problem) return c.json({ detail: problem }, 403)
      const got = await stores()
      if (typeof got === 'string') return c.json({ detail: got }, 503)
      const id = c.req.param('id') ?? ''
      try {
        const session = await got.sessions.get(id, BROWSER_USER)
        if (kind === 'write') {
          try {
            writable(session)
          } catch (err) {
            if (action && err instanceof SessionError) await auditRefused(c, action, session.id, `${err.code}: ${err.message}`)
            throw err
          }
        }
        return await fn(c, got, { kind: 'session', sessionId: session.id }, session.id)
      } catch (err) {
        if (err instanceof SessionError) return c.json({ detail: err.message }, err.status)
        if (err instanceof WorkspaceError) return c.json({ detail: err.message }, STATUS_OF[err.code])
        throw err
      }
    }
  }

  /** As routes/sessions.ts's PATCH: the owner's only, and never an archived session's. */
  function writable(session: SessionRecord): void {
    if (!sameOwner(BROWSER_USER, session.owner)) {
      throw new SessionError('forbidden', `session ${session.id} is controlled by ${session.owner.label}; take it over first`)
    }
    if (session.archivedAt !== null) throw archivedError(session.id)
  }

  function audited(c: Context, action: string, sessionId: string, outcome: AuditOutcome, summary: string): Promise<void> {
    return (
      deps.audit?.record({
        kind: 'resource',
        action,
        surface: 'http',
        actor: UI_ACTOR,
        outcome,
        clientIp: deps.remoteAddress(c),
        sessionId,
        inputSummary: summary,
      }) ?? Promise.resolve()
    )
  }

  /** A refusal's audit row; its own failure is logged, never put in place of the refusal. */
  async function auditRefused(c: Context, action: string, sessionId: string, summary: string): Promise<void> {
    try {
      await audited(c, action, sessionId, 'refused', summary)
    } catch (err) {
      console.error(`the ${action} refusal could not be audited: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  /** The upload's body cap: the per-file cap and the multipart around it, read at each request, after the guard. */
  const uploadLimit: MiddlewareHandler = async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'session file changes')
    if (problem) return c.json({ detail: problem }, 403)
    const got = await stores()
    if (typeof got === 'string') return c.json({ detail: got }, 503)
    const { fileMaxBytes } = await got.files.limits()
    return bodyLimit({
      maxSize: fileMaxBytes + MULTIPART_SLACK,
      onError: (ctx) => ctx.json({ detail: `the upload is larger than a workspace file may be (${fileMaxBytes} bytes)` }, 413),
    })(c, next)
  }

  app.get(
    `${BASE}/:id/files`,
    route('read', async (c, { files }, ws) => {
      const [list, usage, limits] = await Promise.all([files.list(ws), files.usage(ws), files.limits()])
      return c.json({
        files: list.map(fileView),
        usage,
        limits: { file_max_bytes: limits.fileMaxBytes, workspace_max_bytes: limits.workspaceMaxBytes },
      })
    }),
  )

  app.get(
    `${BASE}/:id/files/*`,
    route('read', async (c, { files }, ws) => {
      const name = workspacePath(filePathOf(c) ?? '')
      const file = await files.read(ws, name)
      if (!file) throw new WorkspaceError('not_found', `no file ${name} in this session's workspace`)
      // The last segment is a checked path segment: letters, digits, "_", "." and "-" only.
      const filename = name.slice(name.lastIndexOf('/') + 1)
      return c.body(new Uint8Array(file.bytes), 200, {
        'Content-Type': downloadType(file.mediaType),
        'Content-Disposition': `attachment; filename="${filename}"`,
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'none'; sandbox",
        'Cross-Origin-Resource-Policy': 'same-origin',
        'Cache-Control': 'private, no-store',
      })
    }),
  )

  app.post(
    `${BASE}/:id/files`,
    uploadLimit,
    route('write', async (c, { sessions, files }, ws, sessionId) => {
      const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
      if (type !== 'multipart/form-data') return c.json({ detail: 'the upload must be multipart/form-data' }, 415)
      let form: Record<string, string | File | (string | File)[]>
      try {
        form = await c.req.parseBody({ all: true })
      } catch {
        return c.json({ detail: 'the multipart body could not be read' }, 400)
      }
      const file = form.file
      const rawPath = form.path
      const expected = form.expected_sha256
      if (!(file instanceof File)) return c.json({ detail: 'file: one file is required' }, 400)
      if (typeof rawPath !== 'string') return c.json({ detail: 'path: one workspace path is required' }, 400)
      if (expected !== undefined && (typeof expected !== 'string' || !SHA256.test(expected))) {
        return c.json({ detail: 'expected_sha256: a lowercase hex sha256' }, 400)
      }
      // A part with no type of its own arrives as application/octet-stream: the store then guesses.
      const declared = cleanMediaType(file.type)
      const mediaType = declared === 'application/octet-stream' ? undefined : declared
      let name = rawPath
      let stored: WorkspaceFileInfo
      // The event commits with the file (#2469); followers hear of it once it has.
      let logged: { events: ServerEvent[]; seqs: number[] } | undefined
      try {
        name = workspacePath(rawPath)
        const bytes = new Uint8Array(await file.arrayBuffer())
        stored = await files.write(ws, name, bytes, {
          ...(mediaType ? { mediaType } : {}),
          source: { kind: 'upload', by: 'browser', ...(file.name ? { filename: file.name.slice(0, 255) } : {}) },
          // A new path must not exist yet; replacing one needs the sha256 it has now.
          expectedSha256: expected ?? null,
          alongside: async (tx, file) => {
            const events = [
              event({
                type: 'workspace.file',
                sessionId,
                path: file.path,
                change: 'uploaded',
                by: BROWSER_USER,
                size: file.size,
                mediaType: file.mediaType,
                sha256: file.sha256,
              }),
            ]
            logged = { events, seqs: await sessions.events.append(sessionId, events, tx) }
          },
        })
      } catch (err) {
        if (err instanceof WorkspaceError) await auditRefused(c, 'workspace_upload', sessionId, `${JSON.stringify(name.slice(0, 200))}: ${err.code}`)
        throw err
      }
      if (logged) sessions.events.committed(sessionId, logged.events, logged.seqs)
      await audited(c, 'workspace_upload', sessionId, 'ok', `${name} (${stored.size} bytes, ${stored.mediaType}, sha256 ${stored.sha256})`)
      return c.json({ file: fileView(stored) }, expected === undefined ? 201 : 200)
    }, 'workspace_upload'),
  )

  app.delete(
    `${BASE}/:id/files/*`,
    route('write', async (c, { sessions, files }, ws, sessionId) => {
      const expected = c.req.query('expected_sha256')
      if (expected !== undefined && !SHA256.test(expected)) {
        return c.json({ detail: 'expected_sha256: a lowercase hex sha256' }, 400)
      }
      let name = filePathOf(c) ?? ''
      let logged: { events: ServerEvent[]; seqs: number[] } | undefined
      try {
        name = workspacePath(name)
        const checked = name
        const alongside = async (tx: TransactionSql) => {
          const events = [event({ type: 'workspace.file', sessionId, path: checked, change: 'deleted', by: BROWSER_USER })]
          logged = { events, seqs: await sessions.events.append(sessionId, events, tx) }
        }
        if (!(await files.delete(ws, name, expected === undefined ? { alongside } : { expectedSha256: expected, alongside }))) {
          throw new WorkspaceError('not_found', `no file ${name} in this session's workspace`)
        }
      } catch (err) {
        if (err instanceof WorkspaceError) await auditRefused(c, 'workspace_delete', sessionId, `${JSON.stringify(name.slice(0, 200))}: ${err.code}`)
        throw err
      }
      if (logged) sessions.events.committed(sessionId, logged.events, logged.seqs)
      await audited(c, 'workspace_delete', sessionId, 'ok', name)
      return c.body(null, 204)
    }, 'workspace_delete'),
  )
}

/** The panel's Files (#2329). `deps.workspace` is declared above; `sessions` and `audit` in app.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerSessionFileRoutes(app, {
      sessions: deps.sessions,
      workspace: deps.workspace,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      audit: deps.audit,
    })
  },
}
