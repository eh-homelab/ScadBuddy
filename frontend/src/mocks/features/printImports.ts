import { HttpResponse, http } from 'msw'
import type { FoundSource, ImportedPrint, ModelSummary, PrintDetail, PrintImportRequest, PrintSources } from '../../api/types'
import { prints } from '../fixtures'
import { addMockModel, mockModels, problem } from '../handlers'
import { archiveImports, pastPrint, type PastPrint } from './orders/pastPrints'

/**
 * #4698 — "Import print" (`api/print_imports.py`) over the past prints of
 * `orders/pastPrints.ts`: every archive has its sliced file; 9104 (Benchy) also has its
 * slicer project and a library file of the same name; 9105 (Vase) has no slicer project.
 * The printer's storage (`printer=true`) holds the archive's file in `/cache`. An import
 * adds the archive to the prints list as a print of the model (`GET /prints?slug=`).
 */

const SLICER_PROJECTS = new Set([9104])
const LIBRARY_MATCHES: Record<number, { id: number; name: string }> = { 9104: { id: 501, name: 'benchy.3mf' } }
/** #6394 — the files of ScadBuddy's own library the job's name suggests, by archive. */
export const SUGGESTED_FILES: Record<number, { id: number; filename: string; folder_id: number | null }[]> = {
  9104: [{ id: 1_000_000_501, filename: 'benchy_hull.3mf', folder_id: null }],
}
/** #6394 — the native library files an import may target. */
const NATIVE_FILES = new Set([1_000_000_501, 1_000_000_502])

/** The prints the imports added, so `reset` takes them out again. */
const added: PrintDetail[] = []

export function reset(): void {
  for (const print of added) {
    const at = prints.indexOf(print)
    if (at >= 0) prints.splice(at, 1)
  }
  added.length = 0
}

function sourcesOf(archive: PastPrint, printer: boolean): FoundSource[] {
  const stem = archive.filename.replace(/\.gcode\.3mf$|\.3mf$/, '')
  const found: FoundSource[] = [
    { kind: 'archive', name: archive.filename, size: 2_091_667, modified_at: archive.completed_at ?? null, available: true, sliced: true, preselected: false },
    SLICER_PROJECTS.has(archive.id)
      ? { kind: 'archive_source', name: `${stem}.3mf`, size: 412_330, available: true, sliced: false, preselected: false }
      : { kind: 'archive_source', available: false, reason: 'No slicer project is attached to this archive', sliced: false, preselected: false },
  ]
  const library = LIBRARY_MATCHES[archive.id]
  if (library) {
    found.push({ kind: 'library', name: library.name, size: 398_112, library_file_id: library.id, matched_by: 'name', available: true, sliced: false, preselected: false })
  }
  if (printer) {
    found.push({ kind: 'printer', name: archive.filename, path: `/cache/${archive.filename}`, size: 2_091_667, available: true, sliced: true, preselected: false })
  }
  const usable = found.filter((s) => s.available)
  if (usable.length === 1 && usable[0]) usable[0].preselected = true
  return found
}

/** What an import linked the archive to: a model's slug, or `library:<file id>` (#6394). */
function linkedSlug(archive: PastPrint): string | null {
  return archiveImports.get(archive.id) ?? null
}

function suggested(archive: PastPrint): { slug: string; name: string }[] {
  const words = new Set((archive.print_name ?? '').toLowerCase().split(/[^a-z0-9]+/).filter(Boolean))
  return mockModels()
    .filter((m) => m.slug.split('-').every((part) => words.has(part)))
    .slice(0, 5)
    .map((m) => ({ slug: m.slug, name: m.name }))
}

function importedPrint(archive: PastPrint, slug: string | null, record: ImportedPrint['imported']): PrintDetail {
  const base = prints.find((p) => p.status === 'completed') ?? prints[0]
  if (!base) throw new Error('the print fixtures are empty')
  return {
    ...structuredClone(base),
    archive_id: archive.id,
    output_id: null,
    slug,
    output_name: archive.print_name ?? null,
    printer_id: archive.printer_id ?? null,
    started_at: archive.created_at ?? null,
    completed_at: archive.completed_at ?? null,
    status: archive.status ?? 'completed',
    params_diff: {},
    provenance: null,
    files: [],
    imported: record,
    links: { bambuddy_url: null, customize_url: slug ? `/m/${slug}` : null },
  }
}

export const handlers = [
  http.get('/api/v1/print-imports/:archiveId/sources', ({ params, request }) => {
    const archive = pastPrint(Number(params['archiveId']))
    if (!archive) return problem(404, 'Not Found', `Bambuddy has no archive ${String(params['archiveId'])}`)
    const printer = new URL(request.url).searchParams.get('printer') === 'true'
    const slug = linkedSlug(archive)
    const fileTarget = slug?.startsWith('library:') ? Number(slug.slice('library:'.length)) : null
    const linked =
      fileTarget !== null
        ? { subject: `library:${fileTarget}`, library_file_id: fileTarget }
        : slug !== null
        ? { subject: `model:${slug}`, slug }
        : archive.output_id !== undefined
          ? { subject: `output:${archive.output_id}`, output_id: archive.output_id }
          : archive.library_file_id !== undefined
            ? { subject: `library:${archive.library_file_id}`, library_file_id: archive.library_file_id }
            : null
    return HttpResponse.json({
      archive_id: archive.id,
      print_name: archive.print_name ?? null,
      filename: archive.filename,
      status: archive.status ?? null,
      printer_id: archive.printer_id ?? null,
      linked,
      imported: null,
      sources: linked ? [] : sourcesOf(archive, printer),
      suggested_models: suggested(archive),
      suggested_library_files: SUGGESTED_FILES[archive.id] ?? [],
    } satisfies PrintSources)
  }),

  http.post('/api/v1/print-imports/:archiveId', async ({ params, request }) => {
    const archive = pastPrint(Number(params['archiveId']))
    if (!archive) return problem(404, 'Not Found', `Bambuddy has no archive ${String(params['archiveId'])}`)
    const body = (await request.json()) as PrintImportRequest
    if ([body.slug, body.new_model, body.library_file_id].filter((t) => t != null).length !== 1) {
      return problem(422, 'Unprocessable Content', 'name exactly one of slug, new_model and library_file_id')
    }
    if (body.new_model && !request.headers.get('Idempotency-Key')) return problem(428, 'Precondition Required', 'send an Idempotency-Key')
    const fileId = body.library_file_id ?? null
    const target =
      fileId !== null
        ? `library:${fileId}`
        : (body.slug ?? (body.new_model?.name ?? '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, ''))
    const slug = fileId === null ? target : null
    const answer = (record: ImportedPrint['imported'], created: boolean, madeNow: boolean): ImportedPrint => ({
      archive_id: archive.id,
      subject: fileId === null ? `model:${target}` : target,
      slug,
      library_file_id: fileId,
      print_url: `/api/v1/prints/${archive.id}`,
      created_model: created,
      made_now: madeNow,
      imported: record,
    })
    const owner = linkedSlug(archive)
    if (owner !== null || archive.output_id !== undefined || archive.library_file_id !== undefined) {
      if (owner === target) {
        const print = prints.find((p) => p.archive_id === archive.id)
        if (print?.imported) return HttpResponse.json(answer(print.imported, print.imported.created_model ?? false, false))
      }
      return problem(409, 'Conflict', `archive ${archive.id} is a print of another subject already`)
    }
    const source = sourcesOf(archive, body.source.kind === 'printer').find(
      (s) => s.kind === body.source.kind && (s.library_file_id ?? null) === (body.source.library_file_id ?? null) && (s.path ?? null) === (body.source.path ?? null),
    )
    if (!source) return problem(422, 'Unprocessable Content', 'that source was not found for this archive')
    if (!source.available) return problem(409, 'Conflict', source.reason ?? 'that source cannot be read')
    let created = false
    if (body.new_model) {
      if (source.sliced) return problem(422, 'Unprocessable Content', `${source.name ?? 'the file'} is a sliced file, which has no meshes to open as a model`)
      if (mockModels().some((m) => m.slug === target)) return problem(409, 'Conflict', `a model named '${target}' already exists`)
      const model: ModelSummary = {
        slug: target,
        name: body.new_model.name,
        description: body.new_model.description ?? '',
        tags: ['mesh'],
        updated_at: new Date().toISOString(),
        has_thumbnail: false,
        thumbnail_source: null,
        has_readme: false,
        origin: 'mine',
        inputs_version: 0,
        mesh_source: { kind: 'upload', name: source.name ?? `print-${archive.id}.3mf`, sha256: '0'.repeat(64) },
      }
      addMockModel(model)
      created = true
    } else if (fileId !== null) {
      if (!NATIVE_FILES.has(fileId)) return problem(404, 'Not Found', `ScadBuddy's own library has no file ${fileId}`)
    } else if (!mockModels().some((m) => m.slug === target)) {
      return problem(404, 'Not Found', `no model '${target}'`)
    }
    const record: ImportedPrint['imported'] = {
      archive_id: archive.id,
      subject: fileId === null ? `model:${target}` : target,
      slug,
      source: source.kind,
      library_file_id: source.kind === 'printer' ? 601 : (source.library_file_id ?? null),
      printer_id: source.kind === 'printer' ? (archive.printer_id ?? null) : null,
      printer_path: source.path ?? null,
      filename: source.name ?? `print-${archive.id}.3mf`,
      size: source.size ?? null,
      sha256: null,
      imported_at: new Date().toISOString(),
      created_model: created,
      content_hash: null,
    }
    archiveImports.set(archive.id, target)
    const print = importedPrint(archive, slug, record)
    if (fileId !== null) print.library_file_id = fileId
    prints.unshift(print)
    added.push(print)
    return HttpResponse.json(answer(record, created, true), { status: 201 })
  }),
]

