import { HttpResponse, http } from 'msw'
import type { MeshModelCreate, ModelSummary } from '../../api/types'
import { addMockModel, mockModels, problem } from '../handlers'
import { libraryFiles } from './library'

/**
 * #2146 — `POST /models/from-mesh`: a library file opened as an editable mesh model;
 * `POST /models/from-mesh/upload` (#4703) opens an uploaded one.
 * Arrange makes one as the home of a library file's arranged plates (#3638). The model
 * records the file it came from (`mesh_source`), as the API's does.
 */
export const handlers = [
  http.post('/api/v1/models/from-mesh', async ({ request }) => {
    const body = (await request.json()) as MeshModelCreate
    const slug = body.name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/(^-|-$)/g, '')
    if (!slug) return problem(422, 'Unprocessable Content', `${JSON.stringify(body.name)} does not yield a usable slug`)
    if (mockModels().some((m) => m.slug === slug)) {
      return problem(409, 'Conflict', `a model named '${slug}' already exists`)
    }
    const file = libraryFiles.find((f) => f.id === body.library_file_id)
    const model: ModelSummary = {
      slug,
      name: body.name,
      description: body.description ?? '',
      tags: ['mesh'],
      updated_at: new Date().toISOString(),
      has_thumbnail: false,
      thumbnail_source: null,
      has_readme: false,
      origin: 'mine',
      inputs_version: 0,
      mesh_source:
        body.library_file_id != null
          ? {
              kind: 'bambuddy_library',
              name: file?.filename ?? `file ${body.library_file_id}`,
              sha256: '0'.repeat(64),
              file_id: body.library_file_id,
            }
          : null,
    }
    addMockModel(model)
    return HttpResponse.json(model, { status: 201 })
  }),

  // #4703 — an uploaded 3MF or STL opened as a mesh model; its type is its name's extension.
  http.post('/api/v1/models/from-mesh/upload', async ({ request }) => {
    const form = await request.formData()
    const file = form.get('file')
    if (!(file instanceof File) || file.size === 0) return problem(422, 'Unprocessable Content', 'send the mesh as the `file` part')
    if (!/\.(3mf|stl)$/i.test(file.name)) return problem(415, 'Unsupported Media Type', 'a mesh model opens a .3mf or an .stl')
    const rawName = form.get('name')
    const name = typeof rawName === 'string' && rawName ? rawName : file.name.replace(/\.(3mf|stl)$/i, '')
    const slug = name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/(^-|-$)/g, '')
    if (!slug) return problem(422, 'Unprocessable Content', `${JSON.stringify(name)} does not yield a usable slug`)
    if (mockModels().some((m) => m.slug === slug)) return problem(409, 'Conflict', `a model named '${slug}' already exists`)
    const model: ModelSummary = {
      slug,
      name,
      description: '',
      tags: ['mesh'],
      updated_at: new Date().toISOString(),
      has_thumbnail: false,
      thumbnail_source: null,
      has_readme: false,
      origin: 'mine',
      inputs_version: 0,
      mesh_source: { kind: 'upload', name: file.name, sha256: '0'.repeat(64) },
    }
    addMockModel(model)
    return HttpResponse.json(model, { status: 201 })
  }),
]
