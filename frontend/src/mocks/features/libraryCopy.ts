import { HttpResponse, http } from 'msw'
import type { LibraryCopyFileResult, LibraryCopyRequest, LibraryCopyStatus, LibraryEntry, LibraryFolderView, LibraryListing } from '../../api/types'
import { problem } from '../handlers'
import { libraryFiles } from './library'

/**
 * #5355 — copies between Bambuddy's library and ScadBuddy's own. The mocked page serves
 * `library.ts`'s files as ScadBuddy's own; Bambuddy's library, browsed as a source, is a
 * small tree of its own. A started copy is `running` for one read, then done: each
 * selected file copied, but one whose bytes the target "already holds" (`SAME_HASH`)
 * and one that fails (`FAILS`). While it runs, `FAILS` is pending: its try 2 failed, with
 * the reason, and its try 3 runs.
 */

const base = '/api/v1'

/** A Bambuddy file the target already holds: skipped. */
export const SAME_HASH = 501
/** A Bambuddy file whose copy fails. */
export const FAILS = 502

export const bambuddyFolders: LibraryFolderView[] = [
  { id: 20, name: 'Dinos', parent_id: null, depth: 0, file_count: 3 },
  { id: 21, name: 'Rex', parent_id: 20, depth: 1, file_count: 0 },
  { id: 22, name: 'Keychains', parent_id: null, depth: 0, file_count: 0 },
]

function file(id: number, filename: string, folder_id: number | null): LibraryEntry {
  return {
    id,
    filename,
    file_type: filename.endsWith('.gcode.3mf') ? 'gcode.3mf' : filename.split('.').pop() ?? '',
    folder_id,
    has_thumbnail: false,
    print_count: 0,
    printable: true,
    sliced: filename.endsWith('.gcode.3mf'),
    file_size: 12_345,
    created_at: '2026-11-01T10:00:00Z',
    is_external: false,
  }
}

export const bambuddyFiles = [
  file(500, 'rex.3mf', 20),
  file(SAME_HASH, 'already-here.3mf', 20),
  file(FAILS, 'broken.gcode.3mf', 20),
  file(503, 'loose.stl', null),
]

const copies = new Map<string, { request: LibraryCopyRequest; reads: number }>()
/** Every start's request, in order. */
export const startedCopies: LibraryCopyRequest[] = []

function outcome(request: LibraryCopyRequest, sourceId: number, done: boolean): LibraryCopyFileResult {
  const source = request.direction === 'bb_to_sb' ? bambuddyFiles : libraryFiles
  const name = source.find((f) => f.id === sourceId)?.filename ?? `file ${sourceId}`
  const row = { source_id: sourceId, path: '', name, size: 12_345, reason: null, attempts: 1, reason_try: null, target_id: null, target_folder_id: request.target_folder_id ?? null }
  if (!done && sourceId === FAILS) {
    // Retried: try 2 failed, and try 3 runs. The row says which try failed, and why (#5424).
    return { ...row, status: 'pending', attempts: 3, reason_try: 2, reason: 'HTTP 502: could not reach Bambuddy to download the file: ReadTimeout' }
  }
  if (!done) return { ...row, status: 'pending', attempts: 0 }
  if (sourceId === SAME_HASH) return { ...row, status: 'skipped_same_hash', target_id: 1000000050 }
  if (sourceId === FAILS) return { ...row, status: 'failed', attempts: 3, reason: 'the blob store has no room for it (507)' }
  return { ...row, status: 'copied', target_id: 1000000000 + sourceId }
}

function status(copyId: string, entry: { request: LibraryCopyRequest; reads: number }): LibraryCopyStatus {
  const done = entry.reads > 1
  const folderFiles =
    entry.request.direction === 'bb_to_sb'
      ? bambuddyFiles.filter((f) => typeof f.folder_id === 'number' && (entry.request.folder_ids ?? []).includes(f.folder_id)).map((f) => f.id)
      : libraryFiles.filter((f) => typeof f.folder_id === 'number' && (entry.request.folder_ids ?? []).includes(f.folder_id)).map((f) => f.id)
  const ids = [...new Set([...folderFiles, ...(entry.request.file_ids ?? [])])]
  const files = ids.map((id) => outcome(entry.request, id, done))
  const count = (s: LibraryCopyFileResult['status']) => files.filter((f) => f.status === s).length
  return {
    copy_id: copyId,
    direction: entry.request.direction,
    request: { direction: entry.request.direction, file_ids: entry.request.file_ids ?? [], folder_ids: entry.request.folder_ids ?? [], target_folder_id: entry.request.target_folder_id ?? null },
    status: done ? 'done' : 'running',
    running: !done,
    error: null,
    started_at: '2026-11-06T00:00:00Z',
    finished_at: done ? '2026-11-06T00:00:05Z' : null,
    total: files.length,
    pending: count('pending'),
    copied: count('copied'),
    skipped_same_hash: count('skipped_same_hash'),
    failed: count('failed'),
    files,
  }
}

export const handlers = [
  http.get(`${base}/print/bambuddy-library`, ({ request }) => {
    const raw = new URL(request.url).searchParams.get('folder_id')
    const folderId = raw === null ? null : Number(raw)
    const listing: LibraryListing = {
      folder_id: folderId,
      all: true,
      folders: bambuddyFolders,
      files: bambuddyFiles.filter((f) => f.folder_id === folderId),
      hidden: 0,
    }
    return HttpResponse.json(listing)
  }),

  http.post(`${base}/print/library-copies`, async ({ request }) => {
    const body = (await request.json()) as LibraryCopyRequest
    if ((body.file_ids ?? []).length === 0 && (body.folder_ids ?? []).length === 0) {
      return problem(422, 'Unprocessable Content', 'name at least one file or folder to copy')
    }
    startedCopies.push(body)
    const copyId = `library-copy-${startedCopies.length}`
    copies.set(copyId, { request: body, reads: 0 })
    return HttpResponse.json({ copy_id: copyId }, { status: 202 })
  }),

  http.get(`${base}/print/library-copies`, () =>
    HttpResponse.json([...copies.entries()].map(([id, entry]) => { const { files: _files, ...summary } = status(id, entry); return summary })),
  ),

  http.get(`${base}/print/library-copies/:id`, ({ params }) => {
    const entry = copies.get(String(params.id))
    if (!entry) return problem(404, 'Not Found', `no library copy ${String(params.id)}`)
    entry.reads += 1
    return HttpResponse.json(status(String(params.id), entry))
  }),
]

export function reset(): void {
  copies.clear()
  startedCopies.length = 0
}

