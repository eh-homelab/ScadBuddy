import { HttpResponse, delay, http } from 'msw'
import type {
  AttachResult,
  PrintCheck,
  PrintProgress,
  ProjectAttach,
  ChoicesView,
  FilamentOptions,
  LibraryCopyResult,
  LibraryDeleteResult,
  LibraryEntry,
  LibraryFileObjects,
  LibraryFolderView,
  LibraryListing,
  LibraryMoveResult,
  LibraryRenameResult,
  LibraryRestoreResult,
  ModelPrintChoices,
  OutputPlate,
  PrintRun,
  PrintRunRequest,
  PrintRunResult,
  SlicedProfile,
  BatchChoice,
  NewBatchFile,
  PrintBatch,
} from '../../api/types'
import { choicesView } from '../choices'
import * as fixtures from '../fixtures'
import { keychainGlb } from '../glb'
import { H2C_FILE, H2C_FILENAME, H2C_NOZZLES, h2cFilaments, h2cPlan } from '../h2c'
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
  { id: 1, name: 'MakerWorld', parent_id: null, depth: 0, file_count: 2 },
  { id: 3, name: 'Supplies', parent_id: null, depth: 0, file_count: 0 },
  { id: 4, name: 'Storage', parent_id: 3, depth: 1, file_count: 1 },
  { id: 9, name: 'Bulk', parent_id: null, depth: 0, file_count: 300 },
  // #2670 — a folder linked to a Bambuddy project: a move out of it takes a file out of the project.
  { id: 13, name: 'Lamp', parent_id: null, depth: 0, file_count: 1, project_id: 7, project_name: 'Desk lamp' },
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
    printable: ['3mf', 'stl', 'gcode.3mf'].includes(fileType.toLowerCase()),
    sliced: fileType.toLowerCase() === 'gcode.3mf',
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
  // #2166 — the H2C's two-colour file (Mistletoe Green and Inland Black PLA).
  entry(H2C_FILE, H2C_FILENAME, '3mf', 1),
  entry(46, 'Desiccant_Box.stl', 'stl', 4),
  // #2167 — in Spec/MakerWorld/Work: one to delete, one Bambuddy's key may not (another
  // user added it), and one linked from an external folder.
  entry(120, 'drawer-label.3mf', '3mf', 12),
  entry(121, 'alex-headphone-hook.3mf', '3mf', 12),
  entry(122, 'nas-share-bracket.3mf', '3mf', 12, { is_external: true }),
  ...Array.from({ length: 300 }, (_, n) => entry(2000 + n, `part-${n}.3mf`, '3mf', 9)),
  // #2670 — the Desk lamp project's file.
  entry(130, 'lamp-shade.3mf', '3mf', 13),
]

/** #2671 — what each sliced file was sliced for, as the backend reads it from the file. */
export const SLICED_PROFILES: Record<number, SlicedProfile> = {
  104: {
    printer_model: 'Bambu Lab H2C',
    printer_preset: 'Bambu Lab H2C 0.4 nozzle',
    process: '0.20mm Standard @BBL H2C',
    bed_type: 'Textured PEI Plate',
    nozzle_diameters: ['0.4'],
    plates: [{ index: 1, print_seconds: 2700, weight_g: 9.8, nozzle_diameters: ['0.4'] }],
  },
}

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
  if (!file.printable) {
    return problem(
      422,
      'Unprocessable Content',
      `ScadBuddy prints only 3MF, sliced 3MF and STL files from the library, and ${file.filename} is a ${file.file_type}.`,
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

  /** #2670 — a move keeps each id; Bambuddy skips the file another user added. */
  http.post(`${base}/print/library/move`, async ({ request }) => {
    const { file_ids, folder_id } = (await request.json()) as { file_ids: number[]; folder_id: number | null }
    const target = folder_id ?? null
    const project = (id: number | null) => libraryFolders.find((folder) => folder.id === id)
    const result: LibraryMoveResult = { folder_id: target, moved: [], skipped: [] }
    for (const id of new Set(file_ids)) {
      const file = listedFiles().find((row) => row.id === id)
      if (!file) return problem(404, 'Not Found', `library file ${id} is no longer in Bambuddy's library`)
      if (file.id === NOT_OWNED) {
        result.skipped.push({ id, filename: file.filename, reason: 'Bambuddy did not move it: not the file owner' })
        continue
      }
      const from = project(file.folder_id ?? null)
      const to = project(target)
      const left = from?.project_id != null && from.project_id !== to?.project_id
      moved.set(id, target)
      result.moved.push({
        id,
        filename: file.filename,
        left_project: left ? { id: from.project_id!, name: from.project_name ?? null } : null,
      })
    }
    return HttpResponse.json(result)
  }),

  /** #2670 — each copy a new file, numbered after the mocked library's ids. */
  http.post(`${base}/print/library/copy`, async ({ request }) => {
    const { file_ids, folder_id } = (await request.json()) as { file_ids: number[]; folder_id: number | null }
    const result: LibraryCopyResult = { folder_id: folder_id ?? null, copied: [], skipped: [] }
    for (const id of new Set(file_ids)) {
      const file = listedFiles().find((row) => row.id === id)
      if (!file) return problem(404, 'Not Found', `library file ${id} is no longer in Bambuddy's library`)
      const copy = { ...file, id: 9000 + copies.length, folder_id: folder_id ?? null, print_count: 0 }
      copies.push(copy)
      result.copied.push({ source_id: id, id: copy.id, filename: copy.filename })
    }
    return HttpResponse.json(result)
  }),

  /** #2670 — a new name under the same id; Bambuddy refuses one a printer's SD card would. */
  http.post(`${base}/print/library/rename`, async ({ request }) => {
    const { file_id, filename } = (await request.json()) as { file_id: number; filename: string }
    const file = listedFiles().find((row) => row.id === file_id)
    if (!file) return problem(404, 'Not Found', `library file ${file_id} is no longer in Bambuddy's library`)
    if (/[<>:"|?*]/.test(filename)) {
      return problem(400, 'Bad Request', `Filename contains characters a printer's SD card does not allow: ${filename}`)
    }
    renamed.set(file_id, filename)
    return HttpResponse.json({ id: file_id, filename, previous: file.filename } satisfies LibraryRenameResult)
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
  // #1723 — a plate's mesh, in the file's own colours (the H2C file's green and black).
  http.get(`${base}/print/library/:id/preview.glb`, ({ params, request }) => {
    const colours = Number(params['id']) === H2C_FILE ? ['#3F8E43', '#1A1A1A'] : ['#0047BB', '#FF1493']
    // #2169 — each plate a shape of its own, so the pane visibly changes plate.
    const plate = Number(new URL(request.url).searchParams.get('plate') ?? '1')
    const glb = keychainGlb(colours, plate === 1 ? { x: 60, y: 60, z: 8 } : { x: 30, y: 90, z: 20 })
    return HttpResponse.arrayBuffer(glb.buffer.slice(0) as ArrayBuffer, {
      headers: { 'Content-Type': 'model/gltf-binary' },
    })
  }),

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
    if (fileId === H2C_FILE) {
      return HttpResponse.json({
        ...choicesView,
        printer_id: printerId,
        installed: [{ size: '0.4', flow: 'high_flow', count: 2 }],
        default_nozzles: H2C_NOZZLES,
        filaments: h2cFilaments(printerId),
        model_choices: remembered,
      } satisfies ChoicesView)
    }
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
      sliced: SLICED_PROFILES[fileId] ?? null,
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
    if (Number(params['id']) === H2C_FILE) {
      return HttpResponse.json(h2cFilaments(printerId === null ? null : Number(printerId)))
    }
    return HttpResponse.json({
      ...fixtures.filamentOptions,
      ...(printerId === null ? { nozzles: [] } : {}),
      library_file_id: Number(params['id']),
      printer_id: printerId === null ? null : Number(printerId),
    } satisfies FilamentOptions)
  }),

  // #755 — refuses a missing or unprintable file as the run does; a test that needs a
  // verdict answers this route itself.
  http.post(`${base}/print/library/:id/check`, async ({ params, request }) => {
    const refused = libraryRefusal(Number(params['id']))
    if (refused) return refused
    if (Number(params['id']) === H2C_FILE) {
      const body = (await request.json()) as PrintRunRequest
      return HttpResponse.json({ errors: [], warnings: [], rack: null, nozzle_plan: h2cPlan(body) } satisfies PrintCheck)
    }
    return HttpResponse.json({ errors: [], warnings: [], rack: null } satisfies PrintCheck)
  }),

  http.post(`${base}/print/library/:id/run`, async ({ params, request }) => {
    const fileId = Number(params['id'])
    const refused = libraryRefusal(fileId)
    if (refused) return refused
    const body = (await request.json()) as PrintRunRequest
    await delay(200)
    return HttpResponse.json(startLibraryRun(fileId, body), { status: 202 })
  }),

  ...batchHandlers(),

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
/** #742 — a library run the mock accepted: it has already finished, as a 202's run. */
function startLibraryRun(fileId: number, body: PrintRunRequest): PrintRun {
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
  return run
}

/** #2672 — batch prints, as the backend keeps them in Postgres. */
const batches = new Map<string, PrintBatch>()

function batchHandlers() {
  const found = (id: unknown) => batches.get(String(id))
  const missing = () => problem(404, 'Not Found', 'There is no such batch print.')
  return [
    http.post(`${base}/print/batches`, async ({ request }) => {
      const { files } = (await request.json()) as { files: NewBatchFile[] }
      const batch: PrintBatch = {
        id: `batch${nextNumber()}`,
        status: 'open',
        step: 0,
        created_at: new Date().toISOString(),
        items: files.map((file, position) => ({
          position,
          library_file_id: file.library_file_id,
          filename: file.filename,
          sliced: file.sliced ?? false,
          skipped: false,
          removed: false,
          request: null,
          summary: null,
          problems: [],
          outcome: null,
        })),
      }
      batches.set(batch.id, batch)
      return HttpResponse.json(batch, { status: 201 })
    }),
    http.get(`${base}/print/batches/:id`, ({ params }) => {
      const batch = found(params['id'])
      return batch ? HttpResponse.json(batch) : missing()
    }),
    http.put(`${base}/print/batches/:id/step`, async ({ params, request }) => {
      const batch = found(params['id'])
      if (!batch) return missing()
      const { step } = (await request.json()) as { step: number }
      if (step > batch.items.length) return problem(422, 'Unprocessable Content', 'The batch print has no such step.')
      batch.step = step
      return HttpResponse.json(batch)
    }),
    http.put(`${base}/print/batches/:id/items/:position`, async ({ params, request }) => {
      const batch = found(params['id'])
      if (!batch) return missing()
      if (batch.status !== 'open') return problem(409, 'Conflict', 'This batch print is queued already, so it cannot change.')
      const item = batch.items.find((row) => row.position === Number(params['position']))
      if (!item) return problem(404, 'Not Found', 'The batch print has no such file.')
      const choice = (await request.json()) as BatchChoice
      Object.assign(item, {
        request: choice.request ?? null,
        skipped: choice.skipped ?? false,
        removed: choice.removed ?? false,
        summary: choice.summary ?? null,
        problems: choice.problems ?? [],
      })
      if (choice.step != null) batch.step = choice.step
      return HttpResponse.json(batch)
    }),
    http.post(`${base}/print/batches/:id/queue`, ({ params }) => {
      const batch = found(params['id'])
      if (!batch) return missing()
      for (const item of batch.items) {
        if (item.skipped || item.removed || item.outcome?.status === 'started') continue
        if (!item.request) {
          item.outcome = { status: 'unchosen', run_id: null, detail: 'No choices were made for this file.' }
          continue
        }
        const refused = libraryRefusal(item.library_file_id)
        item.outcome = refused
          ? { status: 'refused', run_id: null, detail: `${item.filename} cannot be printed.` }
          : { status: 'started', run_id: startLibraryRun(item.library_file_id, item.request).id, detail: null }
      }
      // Open until every file kept has started, as the backend leaves it (#2718 review).
      const kept = batch.items.filter((item) => !item.skipped && !item.removed)
      if (kept.every((item) => item.outcome?.status === 'started')) batch.status = 'queued'
      return HttpResponse.json(batch)
    }),
    http.delete(`${base}/print/batches/:id`, ({ params }) =>
      batches.delete(String(params['id'])) ? new HttpResponse(null, { status: 204 }) : missing(),
    ),
  ]
}

export const NOT_OWNED = 121

/** #2670 — the mocked moves, renames and copies, laid over the fixed files. */
const moved = new Map<number, number | null>()
const renamed = new Map<number, string>()
const copies: LibraryEntry[] = []

function listedFiles(): LibraryEntry[] {
  return [...libraryFiles, ...copies]
    .filter((file) => !trashed.has(file.id) && !removed.has(file.id))
    .map((file) => ({
      ...file,
      folder_id: moved.has(file.id) ? moved.get(file.id)! : file.folder_id,
      filename: renamed.get(file.id) ?? file.filename,
    }))
}

export function reset(): void {
  lastQueued.clear()
  batches.clear()
  trashed.clear()
  removed.clear()
  moved.clear()
  renamed.clear()
  copies.length = 0
}
