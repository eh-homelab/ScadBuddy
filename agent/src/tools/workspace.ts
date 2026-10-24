import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import { redact } from '../secrets.js'
import { IMAGE_DATA_MAX } from '../sessions/images.js'
import {
  decodeText,
  sniffImage,
  type SessionFiles,
  type WorkspaceFileInfo,
  WorkspaceError,
  workspaceOf,
  type WorkspaceRef,
  type WorkspaceSource,
  withoutEditCall,
  workspacePath,
} from '../workspace/store.js'
import { readCapped } from './binary.js'
import { ok } from './call.js'
import { outputId, slug } from './common.js'
import { applyEdits, numbered } from './files.js'
import { libraryFileId } from './print.js'
import { defineTool, json, text, ToolError, type Tool, type ToolContext } from './registry.js'

// The assistant's workspace tools (#2329): files of its own, kept per session in
// Postgres (workspace/store.ts), or per principal outside a session (/mcp, a flow
// run). Registry tools, so the same in-process, over /mcp and as durable
// activities. Nothing touches a filesystem: no Claude Code built-in reads or
// writes them, and every byte goes through these tools and their tiers.
//
//   workspace_list, workspace_read                  read
//   workspace_write, workspace_edit, workspace_delete write
//   workspace_save                                  read (it moves bytes inside ScadBuddy only)
//
// A read names the file's sha256; workspace_edit and an optional check on
// workspace_write and workspace_delete are made against it, so a change made in
// between is a conflict, never a silent overwrite. Text reads page as read_file
// does; an image comes back as an image block; any other binary file as its
// metadata. A text read is redacted of the credentials (ToolServices.secrets)
// before the model sees it, and as a tool result its event-log copy is redacted
// of the turn's secrets too (sessions/sdkEvents.ts `scrubForLog`).

const SOURCE = "the assistant's workspace: files it downloaded, saved or wrote, and files the user uploaded"
const DEFAULT_LIMIT = 2000

const path = z
  .string()
  .min(1)
  .max(512)
  .describe('A workspace path: up to 4 "/"-separated parts of letters, digits, "_", "." and "-", e.g. "downloads/part.3mf"')

const sha = z.string().regex(/^[0-9a-f]{64}$/, 'a sha256 as workspace_read or workspace_list gives it')

function filesOf(ctx: ToolContext): { files: SessionFiles; ws: WorkspaceRef } {
  if (!ctx.workspace) throw new ToolError('the workspace is not available here: it needs the agent database')
  return { files: ctx.workspace, ws: workspaceOf(ctx) }
}

/** A WorkspaceError as a ToolError the model reads as it is. */
async function guarded<T>(run: () => Promise<T>): Promise<T> {
  try {
    return await run()
  } catch (err) {
    if (err instanceof WorkspaceError) throw new ToolError(err.message)
    throw err
  }
}

function described(info: WorkspaceFileInfo) {
  return {
    path: info.path,
    media_type: info.mediaType,
    size_bytes: info.size,
    sha256: info.sha256,
    source: info.source,
    updated_at: info.updatedAt.toISOString(),
  }
}

/** Where a session's workspace is, for a result: the session's, or the caller's own for a day. */
function scopeOf(ws: WorkspaceRef): string {
  return ws.kind === 'session' ? 'this session' : 'your own workspace outside any session (files are deleted 24 h after their last write)'
}

/** `bytes` as a name inside the workspace's rules: a backend file's own name made safe. */
export function safeName(name: string): string {
  const clean = name.replace(/[^A-Za-z0-9_.-]+/g, '-').replace(/^[^A-Za-z0-9_]+/, '').slice(0, 128)
  return clean || 'file'
}

const KINDS = [
  'output_3mf',
  'library_file',
  'model_file',
  'model_thumbnail',
  'model_media',
  'print_image',
  'print_timelapse',
  'print_file',
] as const
type Kind = (typeof KINDS)[number]

type SaveArgs = {
  kind: Kind
  path?: string | undefined
  output_id?: string | undefined
  library_file_id?: number | undefined
  slug?: string | undefined
  file_path?: string | undefined
  item_id?: string | undefined
  archive_id?: number | undefined
  photo?: string | undefined
  plate?: number | undefined
  file?: 'sliced' | 'source' | undefined
}

type Pending = Promise<{ data?: unknown; error?: unknown; response: Response }>

function need<T>(value: T | undefined, field: string, kind: Kind): T {
  if (value === undefined) throw new ToolError(`kind "${kind}" needs \`${field}\``)
  return value
}

/** The backend request a save makes, the default file name, and what the source records. */
function saveRequest(args: SaveArgs, ctx: ToolContext): { pending: Pending; what: string; name: string; source: WorkspaceSource; fallbackType: string } {
  const b = ctx.backend
  switch (args.kind) {
    case 'output_3mf': {
      const output_id = need(args.output_id, 'output_id', args.kind)
      return {
        pending: b.GET('/api/v1/outputs/{output_id}/model.3mf', { params: { path: { output_id } }, parseAs: 'stream' }),
        what: `download 3MF of ${output_id}`,
        name: `${output_id}.3mf`,
        source: { tool: 'workspace_save', kind: args.kind, output_id },
        fallbackType: 'model/3mf',
      }
    }
    case 'library_file': {
      const file_id = need(args.library_file_id, 'library_file_id', args.kind)
      return {
        pending: b.GET('/api/v1/print/library/{file_id}/file', { params: { path: { file_id } }, parseAs: 'stream' }),
        what: `download library file ${file_id}`,
        name: `library-${file_id}`,
        source: { tool: 'workspace_save', kind: args.kind, library_file_id: file_id },
        fallbackType: 'application/octet-stream',
      }
    }
    case 'model_file': {
      const s = need(args.slug, 'slug', args.kind)
      const p = need(args.file_path, 'file_path', args.kind).replace(/^(\.\/)+/, '')
      return {
        pending: b.GET('/api/v1/models/{slug}/files/{path}', { params: { path: { slug: s, path: p } }, parseAs: 'stream' }),
        what: `read ${s}/${p}`,
        name: safeName(p.split('/').at(-1) ?? p),
        source: { tool: 'workspace_save', kind: args.kind, slug: s, file_path: p },
        fallbackType: 'application/octet-stream',
      }
    }
    case 'model_thumbnail': {
      const s = need(args.slug, 'slug', args.kind)
      return {
        pending: b.GET('/api/v1/models/{slug}/thumbnail', { params: { path: { slug: s } }, parseAs: 'stream' }),
        what: `get thumbnail of ${s}`,
        name: `${safeName(s)}-thumbnail.png`,
        source: { tool: 'workspace_save', kind: args.kind, slug: s },
        fallbackType: 'image/png',
      }
    }
    case 'model_media': {
      const s = need(args.slug, 'slug', args.kind)
      const item_id = need(args.item_id, 'item_id', args.kind)
      return {
        pending: b.GET('/api/v1/models/{slug}/media/{item_id}', { params: { path: { slug: s, item_id } }, parseAs: 'stream' }),
        what: `get media ${item_id} of ${s}`,
        name: `${safeName(s)}-media-${item_id}`,
        source: { tool: 'workspace_save', kind: args.kind, slug: s, item_id },
        fallbackType: 'application/octet-stream',
      }
    }
    case 'print_image': {
      const archive_id = need(args.archive_id, 'archive_id', args.kind)
      if (args.photo !== undefined && args.plate !== undefined) throw new ToolError('give a photo or a plate, not both')
      const source: WorkspaceSource = { tool: 'workspace_save', kind: args.kind, archive_id }
      if (args.photo !== undefined) {
        const filename = args.photo
        return {
          pending: b.GET('/api/v1/prints/{archive_id}/photos/{filename}', { params: { path: { archive_id, filename } }, parseAs: 'stream' }),
          what: `get photo ${filename} of print ${archive_id}`,
          name: safeName(filename),
          source: { ...source, photo: filename },
          fallbackType: 'image/jpeg',
        }
      }
      if (args.plate !== undefined) {
        const index = args.plate
        return {
          pending: b.GET('/api/v1/prints/{archive_id}/plates/{index}/thumbnail', { params: { path: { archive_id, index } }, parseAs: 'stream' }),
          what: `get plate ${index} image of print ${archive_id}`,
          name: `print-${archive_id}-plate-${index}.png`,
          source: { ...source, plate: index },
          fallbackType: 'image/png',
        }
      }
      return {
        pending: b.GET('/api/v1/prints/{archive_id}/thumbnail', { params: { path: { archive_id } }, parseAs: 'stream' }),
        what: `get thumbnail of print ${archive_id}`,
        name: `print-${archive_id}.png`,
        source,
        fallbackType: 'image/png',
      }
    }
    case 'print_timelapse': {
      const archive_id = need(args.archive_id, 'archive_id', args.kind)
      return {
        pending: b.GET('/api/v1/prints/{archive_id}/timelapse', { params: { path: { archive_id } }, parseAs: 'stream' }),
        what: `get timelapse of print ${archive_id}`,
        name: `print-${archive_id}.mp4`,
        source: { tool: 'workspace_save', kind: args.kind, archive_id },
        fallbackType: 'video/mp4',
      }
    }
    case 'print_file': {
      const archive_id = need(args.archive_id, 'archive_id', args.kind)
      const file = need(args.file, 'file', args.kind)
      return {
        pending:
          file === 'sliced'
            ? b.GET('/api/v1/prints/{archive_id}/files/sliced', { params: { path: { archive_id } }, parseAs: 'stream' })
            : b.GET('/api/v1/prints/{archive_id}/files/source', { params: { path: { archive_id } }, parseAs: 'stream' }),
        what: `download the ${file} file of print ${archive_id}`,
        name: `print-${archive_id}-${file}.3mf`,
        source: { tool: 'workspace_save', kind: args.kind, archive_id, file },
        fallbackType: 'model/3mf',
      }
    }
  }
}

/** A text file's page, as read_file prints it, with where it ends and the sha to edit against. */
function textPage(info: WorkspaceFileInfo, content: string, offset: number, limit: number): CallToolResult {
  const page = numbered(content, offset, limit)
  const where =
    page.total === 0
      ? 'the file is empty'
      : page.to < page.from
        ? `offset is past the end: the file has ${page.total} lines`
        : `lines ${page.from}-${page.to} of ${page.total}${page.to < page.total ? `; continue with offset ${page.to + 1}` : ''}`
  return text(`${page.text}${page.text ? '\n' : ''}[${info.path}: ${where}; ${info.mediaType}; sha256 ${info.sha256}]`)
}

export const workspaceTools: Tool[] = [
  defineTool({
    name: 'workspace_list',
    description:
      "The files in your workspace: this session's files (or, outside a session, your own for 24 h), with each " +
      "one's media type, size, sha256 and where it came from, and how much of the workspace is used. `prefix` " +
      'lists one directory.',
    input: z.object({ prefix: path.optional().describe('Only files under this directory, e.g. "downloads"') }),
    risk: 'read',
    source: SOURCE,
    routes: [],
    handler: async ({ prefix }, ctx) =>
      guarded(async () => {
        const { files, ws } = filesOf(ctx)
        const [list, usage, limits] = await Promise.all([files.list(ws, prefix), files.usage(ws), files.limits()])
        return json({
          workspace: scopeOf(ws),
          files: list.map(described),
          used_bytes: usage.bytes,
          max_bytes: limits.workspaceMaxBytes,
          file_max_bytes: limits.fileMaxBytes,
        })
      }),
  }),

  defineTool({
    name: 'workspace_read',
    description:
      'Read a workspace file. Text comes back as `cat -n` prints it (numbered lines, up to 2000 from `offset`), ' +
      'ending with its sha256: pass that to workspace_edit. An image (PNG, JPEG, GIF, WebP) comes back as an image ' +
      'you can see. Any other binary file (a 3MF, an STL, a video) answers its metadata only.',
    input: z.object({
      path,
      offset: z.number().int().min(1).optional().describe('The line to start from (1-based)'),
      limit: z.number().int().min(1).max(10_000).optional().describe(`How many lines (${DEFAULT_LIMIT} by default)`),
    }),
    risk: 'read',
    source: SOURCE,
    routes: [],
    handler: async ({ path: p, offset, limit }, ctx) =>
      guarded(async () => {
        const { files, ws } = filesOf(ctx)
        const file = await files.read(ws, p)
        if (!file) throw new ToolError(`no file ${workspacePath(p)} in the workspace; workspace_list lists them`)
        const image = sniffImage(file.bytes)
        if (image !== undefined && file.mediaType === image) {
          const base64 = file.bytes.toString('base64')
          if (base64.length <= IMAGE_DATA_MAX) {
            return {
              content: [
                { type: 'image', data: base64, mimeType: image },
                { type: 'text', text: JSON.stringify(described(file)) },
              ],
            }
          }
          return json({ ...described(file), content: 'omitted: the image is larger than an image the model can view (5 MB)' })
        }
        const content = decodeText(file.bytes, file.mediaType)
        // The credentials are redacted before the model sees the text: a saved
        // page or response may quote one back (http_request refuses to send them).
        if (content !== undefined) return textPage(file, redact(content, (await ctx.secrets?.()) ?? []), offset ?? 1, limit ?? DEFAULT_LIMIT)
        return json({ ...described(file), content: 'omitted: a binary file, which is returned as metadata only' })
      }),
  }),

  defineTool({
    name: 'workspace_write',
    description:
      'Create or replace a workspace text file with `content`. Give `expected_sha256` (from workspace_read or ' +
      'workspace_list) to replace only the file you read, or `null` to create a new file only. Prefer workspace_edit ' +
      "to change part of a text file. Text only: a binary file reaches the workspace by the user's upload in the " +
      'assistant panel, http_request `save_to_workspace`, or workspace_save, never as an argument. Answers the stored ' +
      "file's size and sha256.",
    input: z
      .object({
        path,
        content: z.string().describe("The file's whole text (UTF-8)"),
        media_type: z.string().min(1).max(255).optional().describe('The media type; guessed from the bytes and the extension when omitted'),
        expected_sha256: sha
          .nullable()
          .optional()
          .describe("The sha256 the file must have now (else nothing is written), or null for a file that must not exist yet"),
      })
      .strict(),
    risk: 'write',
    source: SOURCE,
    routes: [],
    title: ({ path: p }) => `Write workspace file ${p}`,
    handler: async ({ path: p, content, media_type, expected_sha256 }, ctx) =>
      guarded(async () => {
        const bytes = new TextEncoder().encode(content)
        const { files, ws } = filesOf(ctx)
        const info = await files.write(ws, p, bytes, {
          mediaType: media_type,
          source: { tool: 'workspace_write' },
          expectedSha256: expected_sha256,
        })
        return json({ status: 'written', ...described(info) })
      }),
  }),

  defineTool({
    name: 'workspace_edit',
    description:
      'Replace text in a workspace text file, as Edit does: `old_string` must match exactly once (or set ' +
      '`replace_all`). Made against the `sha256` you read the file at: if it has changed since, nothing is written.',
    input: z.object({
      path,
      old_string: z.string().min(1).describe('The exact text to replace'),
      new_string: z.string().describe('The text to replace it with'),
      replace_all: z.boolean().default(false).describe('Replace every occurrence instead of exactly one'),
      sha256: sha.describe('The sha256 workspace_read gave for the file'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: [],
    title: ({ path: p }) => `Edit workspace file ${p}`,
    handler: async ({ path: p, old_string, new_string, replace_all, sha256: expected }, ctx) =>
      guarded(async () => {
        const { files, ws } = filesOf(ctx)
        const file = await files.read(ws, p)
        if (!file) throw new ToolError(`no file ${workspacePath(p)} in the workspace; workspace_list lists them`)
        if (file.sha256 !== expected) {
          throw new ToolError(`${file.path} changed since it was read: its sha256 is ${file.sha256}; read it again. Nothing was written`)
        }
        const before = decodeText(file.bytes, file.mediaType)
        if (before === undefined) throw new ToolError(`${file.path} is not a text file; workspace_write replaces it whole`)
        const after = applyEdits(before, [{ old_string, new_string, replace_all }])
        const info = await files.write(ws, file.path, new TextEncoder().encode(after), {
          mediaType: file.mediaType,
          source: withoutEditCall(file.source) ?? { tool: 'workspace_edit' },
          expectedSha256: expected,
        })
        return json({ status: 'written', previous_sha256: expected, ...described(info) })
      }),
  }),

  defineTool({
    name: 'workspace_delete',
    description: 'Delete a workspace file. With `expected_sha256`, only when it still has that sha256.',
    input: z.object({ path, expected_sha256: sha.optional() }),
    risk: 'write',
    source: SOURCE,
    routes: [],
    title: ({ path: p }) => `Delete workspace file ${p}`,
    handler: async ({ path: p, expected_sha256 }, ctx) =>
      guarded(async () => {
        const { files, ws } = filesOf(ctx)
        const name = workspacePath(p)
        const deleted = await files.delete(ws, name, { expectedSha256: expected_sha256 })
        if (!deleted) throw new ToolError(`no file ${name} in the workspace; nothing was deleted`)
        return json({ status: 'deleted', path: name })
      }),
  }),

  defineTool({
    name: 'workspace_save',
    description:
      'Save a ScadBuddy file into the workspace, without its bytes passing through you: `kind` output_3mf ' +
      '(`output_id`), library_file (a Bambuddy library file, `library_file_id`), model_file (`slug` and `file_path`: ' +
      'model.scad, a .scad beside it, README.md, any file glob lists), model_thumbnail (`slug`), model_media ' +
      '(`slug`, `item_id` from get_model\'s media), print_image (`archive_id`, and `photo` or `plate`), ' +
      'print_timelapse (`archive_id`) or print_file (`archive_id`, `file` sliced or source). `path` is where it goes ' +
      '(a name from the source when omitted); an existing file there is replaced. workspace_read reads it.',
    input: z.object({
      kind: z.enum(KINDS),
      path: path.optional().describe('Where to save it in the workspace; named after the source when omitted'),
      output_id: outputId.optional(),
      library_file_id: libraryFileId.optional(),
      slug: slug.optional(),
      file_path: z.string().min(1).max(512).optional().describe('model_file: a path in the model\'s directory, e.g. "README.md"'),
      item_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/).optional().describe("model_media: the media item's id"),
      archive_id: z.number().int().min(1).optional().describe("A Bambuddy print archive's id, as list_prints returns it"),
      photo: z
        .string()
        .regex(/^[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp)$/, 'a photo file name from the archive')
        .optional()
        .describe('print_image: one of the archive photos'),
      plate: z.number().int().min(1).optional().describe("print_image: a plate's slicer image (1-based)"),
      file: z.enum(['sliced', 'source']).optional().describe('print_file: which file'),
    }),
    risk: 'read',
    // A library file is read from Bambuddy, as download_3mf's is.
    bambuddyScope: ['Manage Library'],
    source: SOURCE,
    routes: [
      'GET /api/v1/outputs/{output_id}/model.3mf',
      'GET /api/v1/print/library/{file_id}/file',
      'GET /api/v1/models/{slug}/files/{path}',
      'GET /api/v1/models/{slug}/thumbnail',
      'GET /api/v1/models/{slug}/media/{item_id}',
      'GET /api/v1/prints/{archive_id}/photos/{filename}',
      'GET /api/v1/prints/{archive_id}/plates/{index}/thumbnail',
      'GET /api/v1/prints/{archive_id}/thumbnail',
      'GET /api/v1/prints/{archive_id}/timelapse',
      'GET /api/v1/prints/{archive_id}/files/sliced',
      'GET /api/v1/prints/{archive_id}/files/source',
    ],
    title: (args) => `Save ${args.kind.replace(/_/g, ' ')} to the workspace`,
    handler: async (args, ctx) =>
      guarded(async () => {
        const { files, ws } = filesOf(ctx)
        // The path is checked before anything is fetched.
        const target = args.path === undefined ? undefined : workspacePath(args.path)
        const request = saveRequest(args, ctx)
        const name = target ?? workspacePath(request.name)
        const { fileMaxBytes } = await files.limits()
        const result = await request.pending
        const stream = (await ok(Promise.resolve(result), request.what)) as ReadableStream<Uint8Array> | null
        const mediaType = result.response.headers.get('content-type')?.split(';')[0]?.trim() || request.fallbackType
        const lengthHeader = result.response.headers.get('content-length')
        const declared = lengthHeader !== null && /^\d+$/.test(lengthHeader) ? Number(lengthHeader) : undefined
        const read = await readCapped(stream, declared, fileMaxBytes)
        if (!read.inline) {
          throw new ToolError(
            `${request.what}: the file is ${read.size === undefined ? 'larger than' : `${read.size} bytes, over`} the ` +
              `${fileMaxBytes}-byte workspace file limit; nothing was saved`,
          )
        }
        const info = await files.write(ws, name, new Uint8Array(read.bytes), { mediaType, source: request.source })
        return json({ status: 'saved', workspace: scopeOf(ws), ...described(info) })
      }),
  }),
]
