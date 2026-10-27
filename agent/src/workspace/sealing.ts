import { randomBytes } from 'node:crypto'
import type { Sql } from 'postgres'
import { KEK_BYTES, type Kek, openBytes, SealError, sealBytes } from '../secrets.js'
import type { PayloadKeys } from '../temporal/payloadCodec.js'
import type { WorkspaceRef } from './store.js'

// How a workspace file in the blob store is sealed (#4789). Each write makes a random
// file key; the bytes are sealed under it (secrets.ts format) in segments of
// SEGMENT_BYTES, so a reader opens and hands on one segment at a time as the blob
// streams in. Segment i is sealed in the context `workspace-file:<sha256>:<i>:<more|end>`
// (sha256 of the file's bytes): reordered, dropped or cut-short segments fail to open.
//
// The file key is stored in the row (`key_sealed`), wrapped in the context
// `workspace-file-key:<workspace>:<path>:<blob key>`, so a row's key opens only for its
// own workspace, path and blob:
// - a session's file: under the session's data key (`ai_payload_keys`, `session-<id>`,
//   as its transcript is, #3560). Forgetting the session (sessions/forget.ts) deletes
//   that key, so its rows' file keys, and so its blobs, cannot be opened any more;
// - a principal's (outside a session, swept after a day): under the KEK, its id in
//   `key_kek_id`, and re-wrapped at a key rotation (rewrapWorkspaceKeys).
//
// A fork (SessionFiles.copy) opens the parent's file key and wraps it again under the
// child's data key: the child's row names the same blob, and no byte is copied or
// re-sealed (copy-on-write, #4212). The fork's copy then outlives a forget of the parent.

export const SEGMENT_BYTES = 1024 * 1024
/** What sealBytes adds to each segment: the version byte, the IV and the tag. */
export const SEAL_OVERHEAD = 1 + 12 + 16
const SEALED_SEGMENT = SEGMENT_BYTES + SEAL_OVERHEAD

const segmentContext = (digest: string, index: number, last: boolean) => `workspace-file:${digest}:${index}:${last ? 'end' : 'more'}`

export const fileKeyContext = (workspace: string, path: string, blobKey: string) => `workspace-file-key:${workspace}:${path}:${blobKey}`

export function newFileKey(): Buffer {
  return randomBytes(KEK_BYTES)
}

/** `bytes` sealed under `fileKey`, segment by segment; `digest` is their sha256. */
export function sealFile(fileKey: Buffer, bytes: Buffer, digest: string): Buffer {
  const parts: Buffer[] = []
  let index = 0
  for (let offset = 0; ; offset += SEGMENT_BYTES, index += 1) {
    const end = Math.min(offset + SEGMENT_BYTES, bytes.length)
    const last = end >= bytes.length
    parts.push(sealBytes(fileKey, bytes.subarray(offset, end), segmentContext(digest, index, last)))
    if (last) return Buffer.concat(parts)
  }
}

/**
 * The plaintext of a blob `sealFile` wrote, a segment at a time as `sealed` arrives. A
 * SealError when a segment does not open (wrong key, altered, reordered, cut short).
 */
export async function* openFile(fileKey: Buffer, sealed: AsyncIterable<Uint8Array>, digest: string): AsyncGenerator<Buffer> {
  let held = Buffer.alloc(0)
  let index = 0
  for await (const chunk of sealed) {
    held = held.length === 0 ? Buffer.from(chunk) : Buffer.concat([held, chunk])
    // A full segment with bytes after it is not the last.
    while (held.length > SEALED_SEGMENT) {
      yield openBytes(fileKey, held.subarray(0, SEALED_SEGMENT), segmentContext(digest, index, false))
      held = held.subarray(SEALED_SEGMENT)
      index += 1
    }
  }
  if (held.length < SEAL_OVERHEAD) throw new SealError('a sealed workspace file is cut short')
  const tail = openBytes(fileKey, held, segmentContext(digest, index, true))
  if (tail.length > 0) yield tail
}

/** Who wraps a workspace's file keys: a session's data key, or the KEK for a principal's. */
export class WorkspaceSealing {
  readonly #keys: PayloadKeys
  readonly #kek: Kek
  readonly #previous: Kek | undefined
  readonly #sql: Sql

  /** `sql` reads whether a session still stands before its key may be made; `previous` is the KEK being rotated away from. */
  constructor(keys: PayloadKeys, kek: Kek, options: { sql: Sql; previous?: Kek | undefined }) {
    this.#keys = keys
    this.#kek = kek
    this.#previous = options.previous
    this.#sql = options.sql
  }

  /** `fileKey` wrapped for the row (`workspace`, `path`, `blobKey`), and the KEK's id for a principal's. */
  async wrap(
    ws: WorkspaceRef,
    path: string,
    blobKey: string,
    fileKey: Buffer,
  ): Promise<{ sealed: Buffer; kekId: string | null }> {
    const context = fileKeyContext(workspaceKey(ws), path, blobKey)
    if (ws.kind === 'principal') return { sealed: sealBytes(this.#kek.key, fileKey, context), kekId: this.#kek.id }
    // A key is made only for a session that stands: a write racing its deletion or its
    // forget (whose tombstone already stops keyFor) never leaves a key nothing deletes.
    const [standing] = await this.#sql`SELECT 1 FROM ai_sessions WHERE id = ${ws.sessionId}`
    const dek = await this.#keys.keyFor(`session-${ws.sessionId}`, standing !== undefined)
    try {
      return { sealed: sealBytes(dek, fileKey, context), kekId: null }
    } finally {
      dek.fill(0)
    }
  }

  /** The row's file key; SubjectForgotten when its session was forgotten. The caller zeroes it. */
  async unwrap(
    ws: WorkspaceRef,
    path: string,
    blobKey: string,
    sealed: Buffer,
    kekId: string | null,
  ): Promise<Buffer> {
    const context = fileKeyContext(workspaceKey(ws), path, blobKey)
    if (ws.kind === 'principal') {
      const kek = kekId === this.#kek.id ? this.#kek : kekId === this.#previous?.id ? this.#previous : undefined
      if (!kek) throw new SealError(`a workspace file's key was sealed with key ${String(kekId)}, which is not mounted`)
      return openBytes(kek.key, sealed, context)
    }
    const dek = await this.#keys.keyFor(`session-${ws.sessionId}`, false)
    try {
      return openBytes(dek, sealed, context)
    } finally {
      dek.fill(0)
    }
  }
}

function workspaceKey(ws: WorkspaceRef): string {
  return ws.kind === 'session' ? `session:${ws.sessionId}` : `principal:${ws.principalKind}:${ws.principalId}`
}

/** Re-wraps every principal file key sealed under `previous` to `current` (key rotation, main.ts). */
export async function rewrapWorkspaceKeys(sql: Sql, previous: Kek, current: Kek): Promise<{ rewrapped: number; failed: number }> {
  const result = { rewrapped: 0, failed: 0 }
  const rows = await sql<{ workspace: string; path: string; blob_key: string; key_sealed: Buffer }[]>`
    SELECT workspace, path, blob_key, key_sealed FROM ai_session_files WHERE key_kek_id = ${previous.id}`
  for (const row of rows) {
    let fileKey: Buffer | undefined
    try {
      const context = fileKeyContext(row.workspace, row.path, row.blob_key)
      fileKey = openBytes(previous.key, row.key_sealed, context)
      const updated = await sql`
        UPDATE ai_session_files SET key_sealed = ${sealBytes(current.key, fileKey, context)}, key_kek_id = ${current.id}
        WHERE workspace = ${row.workspace} AND path = ${row.path} AND blob_key = ${row.blob_key} AND key_kek_id = ${previous.id}`
      result.rewrapped += updated.count
    } catch (err) {
      if (!(err instanceof SealError)) throw err
      result.failed += 1
    } finally {
      fileKey?.fill(0)
    }
  }
  return result
}
