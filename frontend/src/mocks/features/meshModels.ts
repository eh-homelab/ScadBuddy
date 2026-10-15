import { HttpResponse, http } from 'msw'
import type { MeshModelCreate, ModelSummary } from '../../api/types'
import { addMockModel, mockModels, problem } from '../handlers'
import { libraryFiles } from './library'

/**
 * #2146 — `POST /models/from-mesh`: a library file opened as an editable mesh model.
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
]
