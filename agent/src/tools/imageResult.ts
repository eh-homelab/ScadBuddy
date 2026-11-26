import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { IMAGE_DATA_MAX, IMAGE_MEDIA_TYPES } from '../sessions/images.js'
import { sha256, sniffImage, WorkspaceError, workspaceOf } from '../workspace/store.js'
import { DEFAULT_TOOL_IMAGE_INLINE_MAX } from './imageLimits.js'
import { image, json, type ToolContext } from './registry.js'

// An image a tool shows the model (#7745). Inline as an MCP image block only when
// the model can take it: one of the four types by its signature, and at most the
// inline limit (Settings → Assistant → Images, `tool_image_inline_max_bytes`) and
// `IMAGE_DATA_MAX` of base64. A larger one goes to the session's workspace as a
// file, answered as a pointer `{name, mediaType, size, sha256}` (#2479) that
// workspace_read shows. A type the model cannot view (HEIC, AVIF) is a note; an SVG is its text.
// Never a backend path: a model given one guessed a URL for it (#7727).

/** The bytes an image block of `bytes` takes: its base64. */
const base64Length = (bytes: number) => Math.ceil(bytes / 3) * 4

export type ImageRef = {
  /** A file name for the workspace copy, e.g. `print-12-plate-1.png`. */
  name: string
  /** The backend route the bytes came from, kept as the workspace copy's `source`. */
  path: string
}

/** The current inline limit, in bytes. */
export async function inlineLimit(ctx: Pick<ToolContext, 'toolImageInlineMaxBytes'>): Promise<number> {
  return (await ctx.toolImageInlineMaxBytes?.()) ?? DEFAULT_TOOL_IMAGE_INLINE_MAX
}

/** The image as the model may take it: inline, a workspace pointer, or a note. */
export async function imageOrRef(bytes: Uint8Array, declaredType: string, ctx: ToolContext, ref: ImageRef): Promise<CallToolResult> {
  // An SVG is text the model reads, as it always was here.
  if (declaredType === 'image/svg+xml') return { content: [{ type: 'text', text: Buffer.from(bytes).toString('utf8') }] }
  const type = sniffImage(bytes)
  // A file that is not an image (a sample mesh, a DXF) is kept as it is, so its bytes stay reachable.
  if (type === undefined && !declaredType.startsWith('image/')) return keepFile(bytes, declaredType, ctx, ref)
  if (type === undefined) {
    const shown = (IMAGE_MEDIA_TYPES as readonly string[]).includes(declaredType)
    return json({
      viewable: false,
      media_type: declaredType,
      size_bytes: bytes.byteLength,
      note: shown
        ? `Labelled ${declaredType}, but the bytes are not a valid ${declaredType} image.`
        : `A ${declaredType} image, which you cannot view: only PNG, JPEG, GIF and WebP can be shown to you.`,
    })
  }
  const limit = await inlineLimit(ctx)
  if (bytes.byteLength <= limit && base64Length(bytes.byteLength) <= IMAGE_DATA_MAX) return image(bytes, type)
  const why = `larger than the ${limit}-byte inline limit for images`
  if (!ctx.workspace) {
    return json({ inline: false, reason: why, media_type: type, size_bytes: bytes.byteLength, note: 'There is no workspace to keep it in here.' })
  }
  const safe = ref.name.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 100) || 'image'
  const path = `images/${sha256(bytes).slice(0, 12)}-${safe}`
  try {
    const info = await ctx.workspace.write(workspaceOf(ctx), path, bytes, { mediaType: type, source: { route: ref.path } })
    return json({
      inline: false,
      reason: why,
      workspace_file: { name: info.path, mediaType: info.mediaType, size: info.size, sha256: info.sha256 },
      read_with: 'workspace_read',
      note:
        base64Length(info.size) <= IMAGE_DATA_MAX
          ? 'Kept in your workspace; workspace_read shows it.'
          : 'Kept in your workspace, but too large for you to view (over 5 MB as base64): ask for a smaller copy (a tool may offer a thumbnail).',
    })
  } catch (err) {
    if (err instanceof WorkspaceError) {
      return json({ inline: false, reason: why, media_type: type, size_bytes: bytes.byteLength, note: `Not kept: ${err.message}` })
    }
    throw err
  }
}

/** A file that is not an image, kept in the workspace and answered as a pointer; with no workspace, a note. */
async function keepFile(bytes: Uint8Array, mediaType: string, ctx: ToolContext, ref: ImageRef): Promise<CallToolResult> {
  const notImage = `A ${mediaType} file, not an image.`
  if (!ctx.workspace) return json({ viewable: false, media_type: mediaType, size_bytes: bytes.byteLength, note: `${notImage} There is no workspace to keep it in here.` })
  const safe = ref.name.replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 100) || 'file'
  try {
    const info = await ctx.workspace.write(workspaceOf(ctx), `files/${sha256(bytes).slice(0, 12)}-${safe}`, bytes, { mediaType, source: { route: ref.path } })
    return json({
      viewable: false,
      workspace_file: { name: info.path, mediaType: info.mediaType, size: info.size, sha256: info.sha256 },
      note: `${notImage} Kept in your workspace: workspace_read reads a text file, and a binary one can be attached or uploaded from there.`,
    })
  } catch (err) {
    if (err instanceof WorkspaceError) return json({ viewable: false, media_type: mediaType, size_bytes: bytes.byteLength, note: `${notImage} Not kept: ${err.message}` })
    throw err
  }
}
