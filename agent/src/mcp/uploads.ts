import type { Context, Hono } from 'hono'
import type { AuditOutcome, AuditSink } from '../audit/log.js'
import { authenticate, type McpAuthSettings, mcpTransportProblem, type OidcRuntime } from '../auth/authenticate.js'
import { hasTier, type Principal } from '../auth/principal.js'
import type { TokenStore } from '../auth/tokens.js'
import { cleanMediaType } from '../routes/sessionFiles.js'
import { requestFacts } from '../routes/guard.js'
import {
  guessMediaType,
  mediaTypeOfName,
  type SessionFiles,
  sniffImage,
  WorkspaceError,
  type WorkspaceFileInfo,
  type WorkspaceRef,
  workspacePath,
} from '../workspace/store.js'
import type { McpHttpContext } from './http.js'

// Uploads for /mcp clients (#4550): a local binary file (a thumbnail PNG, an STL)
// goes into the caller's own principal workspace (`principal:<kind>:<id>`,
// workspace/store.ts) as raw bytes, never as base64 in a tool argument (#2479,
// #2510). The answer is the pointer the tools take:
//
//   PUT /mcp/workspace/<path>[?expected_sha256=<hex>]
//       body: the file's bytes (not multipart, not a form), Content-Type optional;
//       `curl -T <file>` sends that (curl's -d and --data-binary send a form type)
//       → 201 {name, mediaType, size, sha256} (200 when it replaced a file)
//
// A later tool call passes `workspace_path: name` and `workspace_sha256: sha256`
// (tools/workspaceUploads.ts). The request passes the gates /mcp passes, in the
// same order (HTTPS, Origin, auth mode, principal; mcp/http.ts), and then:
//
// - The caller needs the `write` tier, as `workspace_write` does.
// - `disabled` auth mode is refused. There every caller is `anonymous`, and only
//   an MCP session id keeps two callers apart, so no credential names one
//   workspace across requests.
// - The path is checked by `workspacePath` (and again by the table's CHECK): up to
//   four segments, each starting with a letter, digit or "_". So "..", an absolute
//   path and a NUL never pass. No path is ever joined to a directory on the pod.
// - The body is read as a stream and stopped at the per-file cap
//   (`workspace_file_max_bytes`); a Content-Length over it is refused unread.
// - The stored type is ScadBuddy's own guess (the image signature, then the
//   extension, then text or not), never the client's. A file whose declared type,
//   extension or first bytes disagree about a raster image type is refused 415.
// - A new path must not exist yet; replacing a file needs the sha256 it has now
//   (`expected_sha256`), as the panel's upload does (routes/sessionFiles.ts).
// - Each write, and each refusal after auth (every status above), is a `resource`
//   audit row.
//
// The file is swept 24 h after its last write (WORKSPACE_TTL_MS), as every file of
// a principal's workspace is.

export const MCP_UPLOAD_PREFIX = '/mcp/workspace/'

const SHA256 = /^[0-9a-f]{64}$/
/** The four raster types sniffImage knows. */
const RASTER: ReadonlySet<string> = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

const STATUS_OF: Record<WorkspaceError['code'], 400 | 404 | 409 | 413 | 503> = {
  invalid_path: 400,
  not_found: 404,
  conflict: 409,
  too_large: 413,
  quota: 413,
  unavailable: 503,
}

export type McpUploadDeps = {
  workspace: SessionFiles | undefined
  audit?: AuditSink | undefined
  /** The configured auth, or fail-closed bearer when it cannot be read (mcp/http.ts `resolveAuth`). */
  resolveAuth: () => Promise<{ settings: McpAuthSettings; tokens: TokenStore }>
  oidc?: OidcRuntime | undefined
}

/** What an upload answers: the pointer a tool call takes. */
export type McpUploadAnswer = { name: string; mediaType: string; size: number; sha256: string }

function problem(c: Context, status: 400 | 403 | 404 | 405 | 409 | 411 | 413 | 415 | 503, error: string, headers: Record<string, string> = {}) {
  return c.json({ error }, status, { 'Cache-Control': 'no-store', ...headers })
}

/** The `<path>` after the prefix in the request's own (still encoded) path, decoded; undefined when it does not decode. */
function uploadPathOf(url: string): string | undefined {
  const pathname = new URL(url).pathname
  if (!pathname.startsWith(MCP_UPLOAD_PREFIX)) return undefined
  try {
    return pathname
      .slice(MCP_UPLOAD_PREFIX.length)
      .split('/')
      .map((segment) => decodeURIComponent(segment))
      .join('/')
  } catch {
    return undefined
  }
}

/** The body's bytes, read chunk by chunk; `too_large` as soon as they pass `max` (the rest is never read). */
export async function readCapped(body: ReadableStream<Uint8Array> | null, max: number): Promise<Uint8Array | 'too_large'> {
  if (!body) return new Uint8Array()
  const reader = body.getReader()
  const parts: Uint8Array[] = []
  let size = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    size += value.byteLength
    if (size > max) {
      await reader.cancel().catch(() => {})
      return 'too_large'
    }
    parts.push(value)
  }
  return Buffer.concat(parts)
}

/**
 * The type a file is stored with: ScadBuddy's own guess. Undefined when the client's
 * declared type, the name's extension or the first bytes disagree about a raster
 * image type: a ".png" that is no PNG, a PNG named ".stl", a JPEG sent as image/png.
 */
export function uploadMediaType(name: string, bytes: Uint8Array, declared: string | undefined): string | undefined {
  const sniffed = sniffImage(bytes)
  const byName = mediaTypeOfName(name)
  // `image/jpg` is not a registered type, but clients send it for JPEG.
  const claimed = declared === 'application/octet-stream' ? undefined : declared === 'image/jpg' ? 'image/jpeg' : declared
  for (const type of [byName, claimed]) {
    if (type === undefined) continue
    if ((RASTER.has(type) || sniffed !== undefined) && type !== sniffed) return undefined
  }
  return guessMediaType(name, bytes)
}

export function mountMcpUploads(app: Hono, deps: McpUploadDeps, http: McpHttpContext): void {
  function audited(c: Context, principal: Principal, outcome: AuditOutcome, summary: string): Promise<void> {
    return (
      deps.audit?.record({
        kind: 'resource',
        action: 'workspace_upload',
        surface: 'mcp',
        actor: { kind: principal.kind, id: principal.id, label: principal.id },
        outcome,
        clientIp: http.remoteAddress(c),
        inputSummary: summary,
      }) ?? Promise.resolve()
    )
  }

  app.all(`${MCP_UPLOAD_PREFIX}*`, async (c) => {
    const request = c.req.raw
    const facts = requestFacts(c, http.remoteAddress)
    const refused = mcpTransportProblem(facts, http.origins, request.url)
    if (refused) return refused
    const { settings, tokens } = await deps.resolveAuth()
    if (request.method !== 'PUT') return problem(c, 405, 'upload a file with PUT', { Allow: 'PUT' })

    const auth = await authenticate(request, { settings, tokens, clientAddress: facts.peer, oidc: deps.oidc })
    if (!auth.ok) return auth.response
    const principal = auth.principal
    let name = uploadPathOf(request.url) ?? ''
    /** A refusal after auth: its audit row, then the answer. */
    const refuse = async (status: Parameters<typeof problem>[1], error: string, why: string) => {
      await audited(c, principal, 'refused', `${JSON.stringify(name.slice(0, 200))}: ${why}`)
      return problem(c, status, error)
    }
    if (principal.kind === 'anonymous') {
      return refuse(
        403,
        'uploads need a credential: /mcp is in disabled auth mode, so no principal owns a workspace across requests. ' +
          'Use a bearer token (Settings, "MCP tokens"), or save the file with http_request save_to_workspace',
        'anonymous',
      )
    }
    if (!hasTier(principal, 'write')) return refuse(403, 'an upload writes to your workspace: it needs a token with the write tier', 'tier')
    if (!deps.workspace) return refuse(503, 'the workspace is not available on this server', 'no_workspace')
    const files = deps.workspace

    const expected = c.req.query('expected_sha256')
    if (expected !== undefined && !SHA256.test(expected)) return refuse(400, 'expected_sha256: a lowercase hex sha256', 'expected_sha256')
    if ((c.req.header('content-encoding') ?? 'identity').trim().toLowerCase() !== 'identity') {
      return refuse(415, 'send the file as it is: Content-Encoding is not accepted', 'content_encoding')
    }
    const rawType = c.req.header('content-type')
    const declared = cleanMediaType(rawType)
    if (declared === 'multipart/form-data' || declared === 'application/x-www-form-urlencoded') {
      return refuse(
        415,
        "send the file's bytes as the body with no form type: curl -T <file> (curl's -d and --data-binary send a form type)",
        'form',
      )
    }
    if (rawType !== undefined && declared === undefined) {
      return refuse(415, `Content-Type ${JSON.stringify(rawType.slice(0, 100))} is not a media type`, 'content_type')
    }

    const ws: WorkspaceRef = { kind: 'principal', principalKind: principal.kind, principalId: principal.id }
    try {
      name = workspacePath(name)
      const length = c.req.header('content-length')
      if (length !== undefined && !/^\d+$/.test(length)) {
        return await refuse(400, `Content-Length ${JSON.stringify(length.slice(0, 40))} is not a byte count`, 'content_length')
      }
      // Refused before the caps are read and before the body: a chunked empty body is caught after the read.
      if (length !== undefined && Number(length) === 0) return await refuse(400, 'the upload is empty: send the file as the body', 'empty')
      const { fileMaxBytes } = await files.limits()
      if (length !== undefined && Number(length) > fileMaxBytes) {
        throw new WorkspaceError('too_large', `the upload is larger than a workspace file may be (${fileMaxBytes} bytes)`)
      }
      const bytes = await readCapped(request.body, fileMaxBytes)
      if (bytes === 'too_large') {
        throw new WorkspaceError('too_large', `the upload is larger than a workspace file may be (${fileMaxBytes} bytes)`)
      }
      if (bytes.byteLength === 0) return await refuse(400, 'the upload is empty: send the file as the body', 'empty')
      const mediaType = uploadMediaType(name, bytes, declared)
      if (mediaType === undefined) {
        return await refuse(415, `${name}: its name, its Content-Type and its first bytes do not agree on one image type`, 'media_type')
      }
      const stored: WorkspaceFileInfo = await files.write(ws, name, bytes, {
        mediaType,
        source: { kind: 'upload', by: 'mcp' },
        // A new path must not exist yet; replacing one needs the sha256 it has now.
        expectedSha256: expected ?? null,
      })
      await audited(c, principal, 'ok', `${stored.path} (${stored.size} bytes, ${stored.mediaType}, sha256 ${stored.sha256})`)
      const answer: McpUploadAnswer = { name: stored.path, mediaType: stored.mediaType, size: stored.size, sha256: stored.sha256 }
      return c.json(answer, expected === undefined ? 201 : 200, { 'Cache-Control': 'no-store' })
    } catch (err) {
      if (!(err instanceof WorkspaceError)) throw err
      return refuse(STATUS_OF[err.code], err.message, err.code)
    }
  })
}
