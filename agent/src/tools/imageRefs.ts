import { z } from 'zod'
import type { BackendClient } from '../api/backend.js'
import {
  AttachmentError,
  type AttachmentRef,
  AttachmentRefSchema,
  type AttachmentStore,
  type ResolvedAttachment,
  unknownAttachment,
} from '../attachments/store.js'
import {
  IMAGE_DATA_MAX,
  IMAGES_DATA_TOTAL_MAX,
  IMAGES_MAX,
  type ImagePreview,
  PREVIEW_DATA_MAX,
  PREVIEW_MEDIA_TYPES,
  sniff,
  type UserImage,
  UserImagesSchema,
} from '../sessions/images.js'
import type { Owner } from '../sessions/protocol.js'
import { readCapped } from './binary.js'
import { outputId, slug, VIEW } from './common.js'
import { ToolError } from './registry.js'

// Images by reference for sessions_send (#1894). A caller names images
// ScadBuddy already holds; the agent fetches each from the backend when it
// starts the turn and checks it exactly as it checks the panel's
// (sessions/images.ts: one of the four types by its signature, IMAGE_DATA_MAX
// each, IMAGES_DATA_TOTAL_MAX together, at most IMAGES_MAX). The bytes never
// travel in the tool call, so:
//
// - the tool input that the event log's `tool.call`, the `/mcp` audit row and
//   an approval summary record (sessions/sdkEvents.ts `scrubForLog`) holds the
//   references only: there is no base64 to scrub;
// - a `/mcp` request stays a few hundred bytes, far under the MCP SDK's 4 MiB
//   body cap (mcp/http.ts), whatever the images weigh;
// - a durable session's workflow (#1056) can carry the references and resolve
//   them in an activity, so image bytes need not enter a Temporal payload.
//
// Kinds are the image routes the read tools already serve (get_output_image,
// draw_view, get_thumbnail, get_asset, get_print_image), so a reference is
// something a caller with `read` could fetch for itself. There is no URL kind:
// fetching a URL a caller chose is outward (spec §8.2) and sessions_send is
// `write`. A web image comes in through fetch_asset (approved, and held to
// the Settings allowlist, #844) and is then sent as `{ kind: 'asset' }`.
//
// One kind is not the backend's: `attachment` (#1941), an image its owner
// uploaded to the agent's attachment store (POST /api/v1/ai/attachments,
// attachments/store.ts). Only that owner can send it, so over /mcp it resolves
// only for the principal that uploaded it; the panel's uploads are the browser
// user's. Its preview is the one uploaded with it, and once the turn has
// started its bytes move into the session (`AttachmentStore.claim`).
//
// Previews (the `user.turn` event's, which the panel shows) come from the
// backend: a view's own 128 px drawing, a media item's thumbnail, or the image
// itself when it is a still within PREVIEW_DATA_MAX. Anything else gets
// NO_PREVIEW, a 1 px grey PNG: the agent has no image library to draw one.

/** The edge of the view the backend draws as a view's preview (VIEW_SIZE allows 64 to 1024). */
export const VIEW_PREVIEW_SIZE = 128

/** The preview of an image the backend has no small copy of: one grey pixel. */
export const NO_PREVIEW: ImagePreview = {
  mediaType: 'image/png',
  data: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGO4AAAA0gDR29kOtwAAAABJRU5ErkJggg==',
}

const mediaId = z.string().regex(/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/, 'must be a media item id from get_model')
const assetId = z.string().regex(/^[0-9a-f]{64}$/, 'must be an asset id: 64 lowercase hex digits')
const archiveId = z.number().int().min(1)

export const ImageRefSchema = z.discriminatedUnion('kind', [
  z.strictObject({
    kind: z.literal('output_thumbnail'),
    output_id: outputId,
    plate: z.number().int().min(1).optional().describe("A plate's cover image (1-based) instead of the thumbnail"),
  }),
  z.strictObject({
    kind: z.literal('output_view'),
    output_id: outputId,
    view: VIEW,
    size: z.number().int().min(64).max(1024).optional().describe('Edge of the square PNG (512 by default)'),
  }),
  z.strictObject({ kind: z.literal('model_thumbnail'), slug }),
  z.strictObject({ kind: z.literal('model_media'), slug, item_id: mediaId }),
  z.strictObject({ kind: z.literal('asset'), slug, asset_id: assetId }),
  z.strictObject({
    kind: z.literal('print_thumbnail'),
    archive_id: archiveId,
    plate: z.number().int().min(1).optional(),
  }),
  AttachmentRefSchema,
])
export type ImageRef = z.infer<typeof ImageRefSchema>

export const ImageRefsSchema = z.array(ImageRefSchema).min(1).max(IMAGES_MAX)

/** What the tool description says about the references, built from the caps. */
export const IMAGE_REFS_DESCRIPTION =
  `Up to ${IMAGES_MAX} images ScadBuddy already holds, by reference, sent to the model before the text: ` +
  "`output_thumbnail` (an output's thumbnail, or a plate's cover with `plate`), `output_view` (a drawn view " +
  "of an output), `model_thumbnail`, `model_media` (a template's media item), `asset` (an uploaded file; " +
  'bring a web image in with fetch_asset first), `print_thumbnail`, and `attachment` (an image this caller ' +
  'uploaded to the assistant\'s attachment store; only its uploader may send it). Each must be a PNG, JPEG, GIF or WebP ' +
  `of at most ${(IMAGE_DATA_MAX / 4) * 3} bytes, and together at most ${(IMAGES_DATA_TOTAL_MAX / 4) * 3} bytes. ` +
  'No inline image data is accepted.'

/** The raw bytes whose base64 is IMAGE_DATA_MAX characters. */
const IMAGE_BYTES_MAX = (IMAGE_DATA_MAX / 4) * 3

type Fetched = { ok: true; data: string } | { ok: false; status: number; detail?: string } | { ok: false; tooLarge: true }

type Pending = Promise<{ error?: unknown; response: Response; data?: unknown }>

async function fetchCapped(pending: Pending, cap: number): Promise<Fetched> {
  const result = await pending
  if (!result.response.ok) {
    const detail = (result.error as { detail?: unknown } | undefined)?.detail
    return { ok: false, status: result.response.status, ...(typeof detail === 'string' ? { detail } : {}) }
  }
  const length = result.response.headers.get('content-length')
  const declared = length !== null && /^\d+$/.test(length) ? Number(length) : undefined
  const read = await readCapped(result.data as ReadableStream<Uint8Array> | null, declared, cap)
  if (!read.inline) return { ok: false, tooLarge: true }
  return { ok: true, data: Buffer.from(read.bytes).toString('base64') }
}

function label(ref: BackendRef): string {
  switch (ref.kind) {
    case 'output_thumbnail':
      return `output ${ref.output_id}${ref.plate === undefined ? ' thumbnail' : ` plate ${ref.plate}`}`
    case 'output_view':
      return `output ${ref.output_id} ${ref.view} view`
    case 'model_thumbnail':
      return `model ${ref.slug} thumbnail`
    case 'model_media':
      return `model ${ref.slug} media ${ref.item_id}`
    case 'asset':
      return `model ${ref.slug} asset ${ref.asset_id}`
    case 'print_thumbnail':
      return `print ${ref.archive_id}${ref.plate === undefined ? ' thumbnail' : ` plate ${ref.plate}`}`
  }
}

type BackendRef = Exclude<ImageRef, { kind: 'attachment' }>

function image(backend: BackendClient, ref: BackendRef): Pending {
  const stream = { parseAs: 'stream' as const }
  switch (ref.kind) {
    case 'output_thumbnail':
      return ref.plate === undefined
        ? backend.GET('/api/v1/outputs/{output_id}/thumbnail', { params: { path: { output_id: ref.output_id } }, ...stream })
        : backend.GET('/api/v1/outputs/{output_id}/plates/{index}/thumbnail', {
            params: { path: { output_id: ref.output_id, index: ref.plate } },
            ...stream,
          })
    case 'output_view':
      return view(backend, ref.output_id, ref.view, ref.size)
    case 'model_thumbnail':
      return backend.GET('/api/v1/models/{slug}/thumbnail', { params: { path: { slug: ref.slug } }, ...stream })
    case 'model_media':
      return backend.GET('/api/v1/models/{slug}/media/{item_id}', {
        params: { path: { slug: ref.slug, item_id: ref.item_id } },
        ...stream,
      })
    case 'asset':
      return backend.GET('/api/v1/models/{slug}/assets/{asset_id}/content', {
        params: { path: { slug: ref.slug, asset_id: ref.asset_id } },
        ...stream,
      })
    case 'print_thumbnail':
      return ref.plate === undefined
        ? backend.GET('/api/v1/prints/{archive_id}/thumbnail', { params: { path: { archive_id: ref.archive_id } }, ...stream })
        : backend.GET('/api/v1/prints/{archive_id}/plates/{index}/thumbnail', {
            params: { path: { archive_id: ref.archive_id, index: ref.plate } },
            ...stream,
          })
  }
}

function view(backend: BackendClient, output_id: string, name: z.infer<typeof VIEW>, size: number | undefined): Pending {
  return backend.GET('/api/v1/outputs/{output_id}/views/{view}.png', {
    params: { path: { output_id, view: name }, query: size === undefined ? {} : { size } },
    parseAs: 'stream',
  })
}

/** The backend's own small copy of `ref`, where it draws one. */
function smallCopy(backend: BackendClient, ref: BackendRef): Pending | undefined {
  if (ref.kind === 'output_view') return view(backend, ref.output_id, ref.view, VIEW_PREVIEW_SIZE)
  if (ref.kind === 'model_media') {
    return backend.GET('/api/v1/models/{slug}/media/{item_id}/thumbnail', {
      params: { path: { slug: ref.slug, item_id: ref.item_id } },
      parseAs: 'stream',
    })
  }
  return undefined
}

function asPreview(data: string): ImagePreview | undefined {
  const type = sniff(data)
  if (data.length > PREVIEW_DATA_MAX || !type || !(PREVIEW_MEDIA_TYPES as readonly string[]).includes(type)) return undefined
  return { mediaType: type as ImagePreview['mediaType'], data }
}

async function preview(backend: BackendClient, ref: BackendRef, data: string): Promise<ImagePreview> {
  const small = smallCopy(backend, ref)
  if (small) {
    // A preview is best effort: a missing or odd small copy falls back, never fails the send.
    const fetched = await fetchCapped(small, (PREVIEW_DATA_MAX / 4) * 3).catch(() => undefined)
    const own = fetched?.ok ? asPreview(fetched.data) : undefined
    if (own) return own
  }
  return asPreview(data) ?? NO_PREVIEW
}

async function resolveOne(backend: BackendClient, ref: BackendRef, index: number): Promise<UserImage> {
  const where = `images[${index}] (${label(ref)})`
  const fetched = await fetchCapped(image(backend, ref), IMAGE_BYTES_MAX)
  if (!fetched.ok) {
    if ('tooLarge' in fetched) throw new ToolError(`${where} is larger than ${IMAGE_BYTES_MAX} bytes`, 413)
    // The backend's reason is untrusted text (#258), and never holds the bytes.
    throw new ToolError(`${where}: fetching it failed (HTTP ${fetched.status})`, fetched.status, fetched.detail)
  }
  const mediaType = sniff(fetched.data)
  if (!mediaType) throw new ToolError(`${where} is not a PNG, JPEG, GIF or WebP image`, 415)
  return { mediaType, data: fetched.data, preview: await preview(backend, ref, fetched.data) }
}

/** Where `attachment` references are read, and for whom: the principal sending the turn. */
export type AttachmentSource = { store: AttachmentStore; owner: Owner }

/** A send's images, and the attachments among them to claim once its turn has started. */
export type ResolvedImages = { images: UserImage[]; attached: ResolvedAttachment[] }

async function attachmentAt(source: AttachmentSource | undefined, ref: AttachmentRef, index: number): Promise<ResolvedAttachment> {
  if (!source) throw new ToolError(`images[${index}]: attachments cannot be sent here`, 400)
  try {
    const [one] = await source.store.resolve(source.owner, [ref])
    return one!
  } catch (err) {
    if (err instanceof AttachmentError) throw new ToolError(unknownAttachment(index, ref.id), 404)
    throw err
  }
}

/**
 * The images `refs` name: the backend's fetched, attachments read for
 * `attachments.owner`, each checked as the panel's are (sessions/images.ts
 * UserImagesSchema). A refusal names the reference and why, never the bytes.
 */
export async function resolveSendImages(
  refs: readonly ImageRef[],
  backend: BackendClient,
  attachments?: AttachmentSource,
): Promise<ResolvedImages> {
  const images: UserImage[] = []
  const attached: ResolvedAttachment[] = []
  // One at a time: at most IMAGES_MAX, and a refusal stops before the rest are read.
  for (const [index, ref] of refs.entries()) {
    if (ref.kind === 'attachment') {
      const one = await attachmentAt(attachments, ref, index)
      attached.push(one)
      images.push(one.image)
    } else {
      images.push(await resolveOne(backend, ref, index))
    }
  }
  const checked = UserImagesSchema.safeParse(images)
  if (!checked.success) {
    // Messages from images.ts name fields and caps only: they never quote the bytes.
    throw new ToolError(`images: ${checked.error.issues.map((i) => `${i.path.join('.') || 'images'}: ${i.message}`).join('; ')}`, 400)
  }
  return { images: checked.data, attached }
}

/** resolveSendImages's images, for a caller with no attachment store: an `attachment` reference is refused. */
export async function resolveImageRefs(refs: readonly ImageRef[], backend: BackendClient): Promise<UserImage[]> {
  return (await resolveSendImages(refs, backend)).images
}
