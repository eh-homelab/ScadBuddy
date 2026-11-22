import { z } from 'zod'
import type { SessionImage } from '../sessions/blobs.js'
import { type ImageMediaType, IMAGES_MAX, type PreviewMediaType, type UserImage } from '../sessions/images.js'
import type { Owner } from '../sessions/protocol.js'

// Images the assistant panel uploads when the user attaches them (#1941), so a
// `user.message` names them, `{ kind: 'attachment', id }`, instead of carrying
// them in the chat socket's frame. The image and its preview are bytes, never
// base64 (#2479).
//
// `ai_attachments` is a staging area, per owner: the panel uploads before any
// session may exist (a new chat's first message makes it). When a turn that
// names attachments has started, `claim` moves their bytes into the session's
// `ai_session_blobs` through `SessionImages.put` (the store #1056's durable
// sessions read images from by `<sha256>.<ext>` name) and deletes the staging
// rows. A row nobody sends expires after ATTACHMENT_TTL_MS and is swept.
// Each owner holds at most OWNER_ROWS_MAX rows and OWNER_BYTES_MAX bytes, so
// the upload route cannot fill the database.
//
// Only an attachment's owner can read, send or delete it: every query names
// the owner, and another owner's id reads as unknown. The bytes are checked by
// the upload route (sessions/images.ts UserImageSchema: type, signature, caps)
// before they are stored. Nothing here logs the bytes, and an error never
// quotes them.
//
// The rows are the API's (#5299 step 7, area `attachments`): `CrudAttachmentStore`
// (crudStore.ts) runs each method on its CRUD routes (backend api/crud_agent_blobs.py), the
// per-owner lock and caps in the API's transaction.

/** How long an attachment waits to be sent. */
export const ATTACHMENT_TTL_MS = 60 * 60 * 1000
/** Attachments one owner may hold, unsent, at once. */
export const OWNER_ROWS_MAX = 24
/** Bytes of unsent attachments one owner may hold (about ten full-size images). */
export const OWNER_BYTES_MAX = 40 * 1024 * 1024
/** How often expired attachments are deleted. */
export const ATTACHMENT_SWEEP_MS = 5 * 60 * 1000

export const AttachmentRefSchema = z.strictObject({
  kind: z.literal('attachment'),
  id: z.uuid().describe('An id POST /api/v1/ai/attachments returned to this caller'),
})
export type AttachmentRef = z.infer<typeof AttachmentRefSchema>

export const AttachmentRefsSchema = z.array(AttachmentRefSchema).min(1).max(IMAGES_MAX)

/**
 * What the upload route answers: the id to send, and the names the image and its
 * preview will have in the session (`<sha256>.<ext>`). Pointers only, never bytes.
 */
export type AttachmentView = {
  id: string
  name: string
  mediaType: ImageMediaType
  preview: { name: string; mediaType: PreviewMediaType }
}

/** An attachment read for a turn: the image for the model, and the blob it becomes. */
export type ResolvedAttachment = { id: string; image: UserImage; blob: SessionImage }

export class AttachmentError extends Error {
  override name = 'AttachmentError'
  readonly code: 'not_found' | 'quota'
  constructor(code: 'not_found' | 'quota', message: string) {
    super(message)
    this.code = code
  }
}

/** Why `images[index]`, attachment `id`, cannot be sent: unknown, expired or another owner's read alike. */
export function unknownAttachment(index: number, id: string): string {
  return `images[${index}]: attachment ${id} is unknown or has expired; attach the image again`
}

export type AttachmentLimits = { ttlMs: number; rowsMax: number; bytesMax: number }

/** The staged attachments: `CrudAttachmentStore`, over the API's routes. */
export interface Attachments {
  put(owner: Owner, image: UserImage): Promise<AttachmentView>
  resolve(owner: Owner, refs: readonly AttachmentRef[]): Promise<ResolvedAttachment[]>
  claim(sessionId: string, owner: Owner, attachments: readonly ResolvedAttachment[]): Promise<void>
  remove(owner: Owner, id: string): Promise<boolean>
  sweep(): Promise<number>
  startSweeper(everyMs: number, options?: { log?: (message: string) => void }): () => void
}

/** Runs `sweep` every `everyMs`; the returned function stops it. */
export function startAttachmentSweeper(sweep: () => Promise<number>, everyMs: number, options: { log?: (message: string) => void } = {}): () => void {
  const timer = setInterval(() => {
    sweep().catch((err: unknown) => {
      options.log?.(`attachments: sweep failed: ${err instanceof Error ? err.message : String(err)}`)
    })
  }, everyMs)
  timer.unref()
  return () => clearInterval(timer)
}
