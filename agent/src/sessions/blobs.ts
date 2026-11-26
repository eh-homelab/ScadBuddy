import { createHash } from 'node:crypto'
import { type BlobMediaType, IMAGE_DATA_MAX, type ImageMediaType, IMAGE_MEDIA_TYPES, sniff, type UserAttachment } from './images.js'
import type { ImageRef, SentImage } from './protocol.js'

// The images a session's tool results carried (#782, design
// docs/superpowers/specs/2026-10-08-friendly-tool-calls-design.md §3.1), in
// `ai_session_blobs`. Claude Code hands every MCP image to the SDK stream as an
// `image` block with its base64 (and writes a copy under its own config
// directory, which on a resumed turn is a temporary one the SDK removes: never
// read that). sdkEvents.ts takes the block; the manager stores the bytes here
// before it logs the `tool.result` that names them; the session route serves
// them. The event carries only `{name, mediaType}`, so the event log every
// watcher replays stays small. The images a user sends with a turn, and their
// previews, are stored here too (`sentImageBlobs`), before the `user.turn`
// event that names them, so any tab loads them by name (#2479). A durable tool
// call's images are stored by its agent-tools activity, which records them under
// the call's tool_use_id (`ai_session_tool_images`); agent-durable's follow_session
// names them on that call's `tool.result` (#2478).
//
// A blob is found by (session, name) and nothing else: no path is ever built
// from a request. The name is the bytes' sha256 and the type's extension, so
// one image twice in a session is one row, and a name that is not that shape
// is refused before the database is asked.
//
// Rows go with their session (ON DELETE CASCADE) and are not pruned before that:
// a session keeps every image its transcript names, so the panel can show any of
// them. The one exception is a queued message's images (#6413): a write made by
// `putMade` answers the names it made and when, and once the message leaves the queue
// unrun, or the queue refuses it, `delete` takes those names back, unless a write
// stored them again since. Every write sets `created_at` (an image already there
// keeps its bytes), and a write of a name waits for the transaction that holds its
// row, so a later write's time is always past `at`: an image another message or turn
// stores, before its event or queue entry names it, is never deleted. A fork copies none (#4212): it reads its ancestors' images that the events it
// reads through its chain name (`get`), never those past its cut (#2150), and
// forgetting a parent copies those into it first.
//
// The rows are the API's (#5299 step 7, area `session-blobs`): `CrudSessionBlobs` (crudBlobs.ts)
// runs each method on its CRUD routes (tools/go/core/internal/blobs), one transaction each.
// A forget copies a fork's images there (backend events.py `copy_fork_blobs`, #5299 step 9,
// part c).

/** The most images one tool result keeps; any past it stay `[image]` in the summary. */
export const RESULT_IMAGES_MAX = 8

const EXTENSION: Record<BlobMediaType, string> = {
  'application/pdf': 'pdf',
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
}

/** What a blob's name must be: lowercase sha256 hex, a dot, one of the four image extensions or `pdf` (#6267). */
export const BLOB_NAME = /^[0-9a-f]{64}\.(png|jpg|gif|webp|pdf)$/

// As images.ts: a plain class, never a group repeated per quartet.
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/

/** An image or a PDF with its bytes, on its way to the session's store (#6267). */
export type SessionBlob = { name: string; mediaType: BlobMediaType; bytes: Buffer }
/** An image with its bytes: what a tool result carries. */
export type SessionImage = ImageRef & { bytes: Buffer }

/**
 * The image an SDK `image` content block holds, if it is one the panel may
 * show: base64, one of the four Messages API types, its bytes starting with
 * that type's signature, and no larger than an image the Messages API takes.
 * Anything else is undefined, and stays a placeholder.
 */
export function imageOfBlock(block: unknown): SessionImage | undefined {
  if (typeof block !== 'object' || block === null) return undefined
  const source = (block as { type?: unknown; source?: unknown }).source
  if ((block as { type?: unknown }).type !== 'image' || typeof source !== 'object' || source === null) return undefined
  const { type, media_type: mediaType, data } = source as { type?: unknown; media_type?: unknown; data?: unknown }
  if (type !== 'base64' || typeof data !== 'string' || typeof mediaType !== 'string') return undefined
  if (!(IMAGE_MEDIA_TYPES as readonly string[]).includes(mediaType)) return undefined
  if (data.length === 0 || data.length > IMAGE_DATA_MAX || data.length % 4 !== 0 || !BASE64.test(data)) return undefined
  const bytes = Buffer.from(data, 'base64')
  if (sniff(bytes) !== mediaType) return undefined
  return sessionImage(mediaType as ImageMediaType, bytes)
}

/** `bytes` as a session image, named by their sha256 and the type's extension. */
export function sessionImage(mediaType: ImageMediaType, bytes: Buffer): SessionImage
export function sessionImage(mediaType: BlobMediaType, bytes: Buffer): SessionBlob
export function sessionImage(mediaType: BlobMediaType, bytes: Buffer): SessionBlob {
  return { name: `${createHash('sha256').update(bytes).digest('hex')}.${EXTENSION[mediaType]}`, mediaType, bytes }
}

/**
 * A turn's images as the blobs to store (each full image, then its preview) and
 * what `user.turn` names them by (#2479).
 */
export function sentImageBlobs(images: readonly UserAttachment[]): { blobs: SessionBlob[]; sent: SentImage[] } {
  const blobs: SessionBlob[] = []
  const sent = images.map((image): SentImage => {
    if (image.mediaType === 'application/pdf') {
      // A PDF has no preview (#6267): the turn names the document only.
      const doc = sessionImage(image.mediaType, image.bytes)
      blobs.push(doc)
      return { name: doc.name, mediaType: image.mediaType, ...(image.fileName ? { fileName: image.fileName } : {}) }
    }
    const full = sessionImage(image.mediaType, image.bytes)
    if (!image.preview) {
      // A server-built turn (#6267) has no preview: the turn names the full image only.
      blobs.push(full)
      return { name: full.name, mediaType: image.mediaType }
    }
    const small = sessionImage(image.preview.mediaType, image.preview.bytes)
    blobs.push(full, small)
    return {
      name: full.name,
      mediaType: image.mediaType,
      preview: { name: small.name, mediaType: image.preview.mediaType },
    }
  })
  return { blobs, sent }
}

export type StoredBlob = { mediaType: ImageMediaType; bytes: Buffer }
/** One of a session's blobs: an image, or a PDF (#6267). */
export type StoredSessionBlob = { mediaType: BlobMediaType; bytes: Buffer }

/**
 * An AgentSession placement's hold on its turn (turnHost.ts): the session's row still names
 * this attempt and this turn, checked `FOR SHARE` in the write's own transaction.
 */
export type TurnFence = { attempt: string; turnId: string }

/**
 * What one write of images made (#6413): the names not there before, and a time no later write
 * of one of them comes before (each write sets `created_at`), which `delete` takes as `before`.
 */
export type MadeImages = { names: string[]; at: string }

/** A session's images: `SessionBlobs` over SQL, `CrudSessionBlobs` over the API's routes. */
export interface SessionImages {
  put(sessionId: string, images: readonly SessionBlob[], toolUseId?: string): Promise<void>
  /**
   * `put`, answering what it made, one entry per write (transaction) that made a name: a queued
   * message's images, which `delete` takes back once it leaves the queue unrun.
   */
  putMade(sessionId: string, images: readonly SessionBlob[]): Promise<MadeImages[]>
  /**
   * Deletes the images one write made, unless a write stored one again since or a tool call names
   * it; the names deleted. With `fence`, nothing unless the fence holds.
   */
  delete(sessionId: string, made: MadeImages, fence?: TurnFence): Promise<string[]>
  /** False when the fence refuses; over the API's routes a batch stored before the refusal stays (crudBlobs.ts). */
  putIf(sessionId: string, images: readonly SessionBlob[], fence: TurnFence): Promise<boolean>
  get(sessionId: string, name: string): Promise<StoredSessionBlob | undefined>
}
