import { HttpResponse, delay, http } from 'msw'
import type {
  AttachResult,
  PrintCheck,
  PrintProgress,
  ProjectAttach,
  ChoicesView,
  FilamentOptions,
  LibraryDeleteResult,
  LibraryEntry,
  LibraryFileObjects,
  LibraryFolderView,
  LibraryListing,
  LibraryRestoreResult,
  ModelPrintChoices,
  OutputPlate,
  PrintRun,
  PrintRunRequest,
  PrintRunResult,
} from '../../api/types'
import { choicesView } from '../choices'
import * as fixtures from '../fixtures'
import {
  mockLibraryChoices,
  mockLibraryOutput,
  mockSettings,
  nextNumber,
  problem,
  recordMockPrintRun,
  setMockLibraryChoices,
} from '../handlers'
import { SLICED_REASON, mockLibraryObjects } from '../libraryObjects'

/**
 * #313 — printing a file already in Bambuddy's library: the listing, a file's plates and
 * images, the dialog's choices (remembered per file), filaments and the run. The
 * remembered choices stay in `handlers.ts` with the others, so "Forget all" drops them.
 */

const base = '/api/v1'

/** The mocked Bambuddy library, shaped like tests/bambuddy/recordings. */
export const libraryFolders: LibraryFolderView[] = [
  { id: 1, name: 'MakerWorld', parent_id: null, depth: 0, file_count: 1 },
  { id: 3, name: 'Supplies', parent_id: null, depth: 0, file_count: 0 },
  { id: 4, name: 'Storage', parent_id: 3, depth: 1, file_count: 1 },
  { id: 9, name: 'Bulk', parent_id: null, depth: 0, file_count: 300 },
  // #2165 — a name repeated under another folder, as the live library has it.
  { id: 10, name: 'Spec', parent_id: null, depth: 0, file_count: 0 },
  { id: 11, name: 'MakerWorld', parent_id: 10, depth: 1, file_count: 0 },
  { id: 12, name: 'Work', parent_id: 11, depth: 2, file_count: 3 },
]

function entry(
  id: number,
  filename: string,
  fileType: string,
  folderId: number | null,
  added: Pick<LibraryEntry, 'file_size' | 'created_at'> & { is_external?: boolean } = {},
): LibraryEntry {
  return {
    id,
    filename,
    file_type: fileType,
    folder_id: folderId,
    has_thumbnail: fileType !== 'stl',
    print_count: 0,
    printable: ['3mf', 'stl'].includes(fileType.toLowerCase()),
    file_size: added.file_size ?? 64_000,
    created_at: added.created_at ?? '2026-09-20T12:00:00Z',
    is_external: added.is_external ?? false,
  }
}

/** #935 — two uploads of one output: the same name and size, three minutes apart. */
const BAG_CLIP = 'bag-clip-3155628dc2bb43d4940fad6dba164efc'

export const libraryFiles: LibraryEntry[] = [
  entry(89, `${BAG_CLIP}.3mf`, '3mf', null, { file_size: 109_795, created_at: '2026-09-26T17:05:45Z' }),
  entry(91, `${BAG_CLIP}.3mf`, '3mf', null, { file_size: 109_795, created_at: '2026-09-26T17:08:42Z' }),
  entry(104, `${BAG_CLIP}.gcode.3mf`, 'gcode.3mf', null, {
    file_size: 864_242,
    created_at: '2026-09-27T17:17:51Z',
  }),
  entry(67, "Clara's Wand.3mf", '3mf', 1),
  entry(46, 'Desiccant_Box.stl', 'stl', 4),
  // #2167 — in Spec/MakerWorld/Work: one to delete, one Bambuddy's key may not (another
  // user added it), and one linked from an external folder.
  entry(120, 'drawer-label.3mf', '3mf', 12),
  entry(121, 'alex-headphone-hook.3mf', '3mf', 12),
  entry(122, 'nas-share-bracket.3mf', '3mf', 12, { is_external: true }),
  ...Array.from({ length: 300 }, (_, n) => entry(2000 + n, `part-${n}.3mf`, '3mf', 9)),
]

/** The two-plate file; every other 3MF is one plate and an STL none. */
export const MULTI_PLATE_FILE = 67

/** What the library routes answer for a file that is gone or not printable. */
function libraryRefusal(fileId: number) {
  const file = libraryFiles.find((row) => row.id === fileId)
  if (!file) {
    return problem(
      404,
      'Not Found',
      `Bambuddy has no such resource when asked to read library file ${fileId}`,
    )
  }
  if (file.file_type === 'gcode.3mf') {
    return problem(422, 'Unprocessable Content', `${file.filename} is sliced already. Print it from Bambuddy.`)
  }
  if (!file.printable) {
    return problem(
      422,
      'Unprocessable Content',
      `ScadBuddy prints only 3MF and STL files from the library, and ${file.filename} is a ${file.file_type}.`,
    )
  }
  return null
}

function pngResponse() {
  const bytes = Uint8Array.from(atob(fixtures.MEDIA_PNG_BASE64), (char) => char.charCodeAt(0))
  return new HttpResponse(bytes, { headers: { 'Content-Type': 'image/png' } })
}

export const handlers = [
  http.get(`${base}/print/library`, ({ request }) => {
    const search = new URL(request.url).searchParams
    const asked = search.get('folder_id')
    const linked = search.get('file_id')
    let folderId = asked === null ? null : Number(asked)
    if (asked === null && linked !== null) {
      const file = listedFiles().find((row) => row.id === Number(linked))
      if (!file) return problem(404, 'Not Found', `Bambuddy has no such resource when asked to read library file ${linked}`)
      folderId = file.folder_id ?? null
    }
    const all = search.get('all') === 'true'
    const here = listedFiles().filter((file) => (file.folder_id ?? null) === folderId)
    const files = all ? here : here.filter((file) => file.file_type?.toLowerCase() === '3mf')
    return HttpResponse.json({
      folder_id: folderId,
      all,
      folders: libraryFolders,
      files: files.map((file) => ({ ...file, output_id: mockLibraryOutput(file.id) ?? null })),
      hidden: here.length - files.length,
    } satisfies LibraryListing)
  }),

  /** #2167 — to the mocked trash; Bambuddy skips the file another user added. */
  http.post(`${base}/print/library/delete`, async ({ request }) => {
    const { file_ids } = (await request.json()) as { file_ids: number[] }
    const ids = [...new Set(file_ids)]
    const files = ids.map((id) => listedFiles().find((row) => row.id === id))
    const absent = ids.find((_, n) => !files[n])
    if (absent !== undefined) return problem(404, 'Not Found', `library file ${absent} is no longer in Bambuddy's library`)
    const result: LibraryDeleteResult = { deleted: [], skipped: [] }
    for (const file of files as LibraryEntry[]) {
      if (file.id === NOT_OWNED) {
        result.skipped.push({ id: file.id, filename: file.filename, reason: "Bambuddy deletes only files its API key's user added" })
        continue
      }
      if (file.is_external) removed.add(file.id)
      else trashed.add(file.id)
      result.deleted.push({ id: file.id, filename: file.filename, trashed: !file.is_external })
    }
    return HttpResponse.json(result)
  }),

  http.post(`${base}/print/library/restore`, async ({ request }) => {
    const { file_ids } = (await request.json()) as { file_ids: number[] }
    const result: LibraryRestoreResult = { restored: [], skipped: [] }
    for (const id of new Set(file_ids)) {
      if (trashed.delete(id)) result.restored.push(id)
      else result.skipped.push({ id, reason: "it is not in Bambuddy's trash" })
    }
    return HttpResponse.json(result)
  }),

  http.get(`${base}/print/library/:id/plates/:index/thumbnail`, () => pngResponse()),
  http.get(`${base}/print/library/:id/thumbnail`, () => pngResponse()),

  http.get(`${base}/print/library/:id/plates`, ({ params }) => {
    const file = libraryFiles.find((row) => row.id === Number(params['id']))
    if (!file) {
      return problem(404, 'Not Found', `Bambuddy has no such resource when asked to read the plates of library file ${params['id']}`)
    }
    if (file.file_type === 'stl') return HttpResponse.json([] satisfies OutputPlate[])
    const count = file.id === MULTI_PLATE_FILE ? 2 : 1
    return HttpResponse.json(
      Array.from({ length: count }, (_, n) => ({ index: n + 1, has_thumbnail: true })) satisfies OutputPlate[],
    )
  }),

  http.get(`${base}/print/library/:id/choices`, ({ params, request }) => {
    const fileId = Number(params['id'])
    const refused = libraryRefusal(fileId)
    if (refused) return refused
    const remembered = mockLibraryChoices(fileId)
    const asked = new URL(request.url).searchParams.get('printer_id')
    const printerId =
      asked !== null ? Number(asked) : (remembered.printer_id ?? choicesView.printer_id ?? null)
    const printerName =
      (choicesView.printers ?? []).find((printer) => printer.id === printerId)?.name ?? null
    return HttpResponse.json({
      ...choicesView,
      printer_id: printerId,
      filaments: {
        ...choicesView.filaments,
        library_file_id: fileId,
        printer_id: printerId,
        printer_name: printerName,
      },
      model_choices: remembered,
    } satisfies ChoicesView)
  }),

  // #1863 — the objects Arrange reads from the file's 3MF; a sliced file is refused.
  http.get(`${base}/print/library/:id/objects`, ({ params }) => {
    const fileId = Number(params['id'])
    const file = libraryFiles.find((row) => row.id === fileId)
    if (!file) return problem(404, 'Not Found', `Bambuddy has no such resource when asked to read library file ${fileId}`)
    const objects = mockLibraryObjects(fileId)
    if (!objects) {
      return problem(
        422,
        'Unprocessable Content',
        `1 library file(s) cannot be arranged: ${file.filename}: ${SLICED_REASON}`,
        { code: 'library_file_not_arrangeable', library_file_ids: [fileId] },
      )
    }
    return HttpResponse.json({ file_id: fileId, filename: file.filename, objects } satisfies LibraryFileObjects)
  }),

  http.put(`${base}/print/library/:id/choices`, async ({ params, request }) => {
    const body = (await request.json()) as ModelPrintChoices
    return HttpResponse.json(setMockLibraryChoices(Number(params['id']), body))
  }),

  http.get(`${base}/print/library/:id/filaments`, ({ params, request }) => {
    const refused = libraryRefusal(Number(params['id']))
    if (refused) return refused
    const printerId = new URL(request.url).searchParams.get('printer_id')
    return HttpResponse.json({
      ...fixtures.filamentOptions,
      ...(printerId === null ? { nozzles: [] } : {}),
      library_file_id: Number(params['id']),
      printer_id: printerId === null ? null : Number(printerId),
    } satisfies FilamentOptions)
  }),

  // #755 — refuses a missing or unprintable file as the run does; a test that needs a
  // verdict answers this route itself.
  http.post(`${base}/print/library/:id/check`, ({ params }) => {
    const refused = libraryRefusal(Number(params['id']))
    if (refused) return refused
    return HttpResponse.json({ errors: [], warnings: [], rack: null } satisfies PrintCheck)
  }),

  http.post(`${base}/print/library/:id/run`, async ({ params, request }) => {
    const fileId = Number(params['id'])
    const refused = libraryRefusal(fileId)
    if (refused) return refused
    const body = (await request.json()) as PrintRunRequest
    await delay(200)
    const result = {
      route: 'slice_queue',
      library_file_id: fileId,
      printer_id: body.printer_id ?? null,
      slice_job_id: nextNumber(),
      sliced_library_file_id: nextNumber(),
      queue_item_ids: [nextNumber()],
      copies: body.copies ?? 1,
      warnings: [],
      project_id: body.project_id ?? null,
      folder_id: null,
      bambuddy_url: `${mockSettings().bambuddy_url}/queue`,
    } satisfies PrintRunResult
    // #742: a 202 with a run, like an output's; this one has already finished.
    const now = new Date().toISOString()
    const run: PrintRun = {
      id: `run-${nextNumber()}`,
      subject: `library:${fileId}`,
      output_id: `library:${fileId}`,
      status: 'succeeded',
      created_at: now,
      finished_at: now,
      result,
      error: null,
      may_have_queued: false,
      repeated: false,
    }
    recordMockPrintRun(run)
    lastQueued.set(fileId, result.queue_item_ids[0] ?? 0)
    return HttpResponse.json(run, { status: 202 })
  }),

  /** #1751 — the file's newest print, queued and waiting, as an output's is. */
  http.get(`${base}/print/library/:id/progress`, ({ params }) => {
    const item = lastQueued.get(Number(params['id']))
    if (item === undefined) return HttpResponse.json(null)
    return HttpResponse.json({
      ...fixtures.queuedSliceProgress,
      queue_item_id: item,
      copies_detail: (fixtures.queuedSliceProgress.copies_detail ?? []).map((copy) => ({ ...copy, queue_entry_id: item })),
    } satisfies PrintProgress)
  }),

  http.post(`${base}/print/library/:id/project`, async ({ params, request }) => {
    const body = (await request.json()) as ProjectAttach
    const item = lastQueued.get(Number(params['id']))
    return HttpResponse.json({
      project_id: body.project_id ?? 0,
      queue_item_ids: body.queue_item_ids?.length ? body.queue_item_ids : item === undefined ? [] : [item],
      archive_ids: [],
    } satisfies AttachResult)
  }),
]

/** #1751 — each library file's newest queue item, by the mocked run that queued it. */
const lastQueued = new Map<number, number>()

/** #2167 — the files the mocked delete moved to the trash, or removed for good. */
const trashed = new Set<number>()
const removed = new Set<number>()
/** The file Bambuddy's key may not delete: another user added it. */
export const NOT_OWNED = 121

function listedFiles(): LibraryEntry[] {
  return libraryFiles.filter((file) => !trashed.has(file.id) && !removed.has(file.id))
}

export function reset(): void {
  lastQueued.clear()
  trashed.clear()
  removed.clear()
}
