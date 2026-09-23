import { HttpResponse, delay, http } from 'msw'
import type { Job, ModelSummary, Output, ParamValue, Settings } from '../api/types'
import { keychainGlb } from './glb'
import * as fixtures from './fixtures'

const base = '/api/v1'

interface MockJob extends Job {
  params: Record<string, ParamValue>
  polls: number
}

const state = {
  models: [...fixtures.models] as ModelSummary[],
  schemas: { ...fixtures.schemas },
  outputs: [...fixtures.outputs] as Output[],
  settings: { ...fixtures.settings } as Settings,
  jobs: new Map<string, MockJob>(),
  sent: new Map<string, { library_file_id: string; queue_item_id?: string }>(),
  seq: 0,
}

/** Reset every mutable fixture. Call between tests. */
export function resetMockState(): void {
  state.models = fixtures.models.map((m) => ({ ...m }))
  state.schemas = { ...fixtures.schemas }
  state.outputs = fixtures.outputs.map((o) => ({ ...o }))
  state.settings = { ...fixtures.settings }
  state.jobs.clear()
  state.sent.clear()
  state.seq = 0
}

function nextId(prefix: string): string {
  state.seq += 1
  return `${prefix}-${String(state.seq).padStart(4, '0')}`
}

function num(params: Record<string, ParamValue>, key: string, fallback: number): number {
  const value = params[key]
  return typeof value === 'number' ? value : fallback
}

function colorsOf(slug: string, params: Record<string, ParamValue>): string[] {
  const schema = state.schemas[slug]
  if (!schema) return []
  const colors = schema.groups
    .flatMap((group) => group.params)
    .filter((param) => param.type === 'color')
    .map((param) => String(params[param.name] ?? param.initial))
  return colors.length > 0 ? colors : ['#9AA4B2']
}

/** Cheap stand-in for the real render: geometry that actually tracks the parameters. */
function bboxOf(params: Record<string, ParamValue>) {
  const name = String(params['name'] ?? 'Model')
  const textSize = num(params, 'text_size', 14)
  const padding = num(params, 'padding', 6)
  const thickness = num(params, 'thickness', 5.2)
  const depth = num(params, 'text_depth', 1.6)
  const unitsX = num(params, 'units_x', 0)
  if (unitsX > 0) {
    return {
      x: round(unitsX * 42),
      y: round(num(params, 'units_y', 1) * 42),
      z: round(num(params, 'height_units', 3) * 7),
    }
  }
  return {
    x: round(Math.max(name.length, 1) * textSize * 0.62 + padding * 2),
    y: round(textSize * 1.8 + padding * 2),
    z: round(thickness + depth),
  }
}

function round(value: number): number {
  return Math.round(value * 10) / 10
}

function jobView(job: MockJob): Job {
  const { params: _params, polls: _polls, ...rest } = job
  return rest
}

function problem(status: number, title: string, detail?: string) {
  return HttpResponse.json(
    { type: 'about:blank', title, status, detail },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

export const handlers = [
  http.get(`${base}/models`, () => HttpResponse.json(state.models)),

  http.post(`${base}/models`, async ({ request }) => {
    const form = await request.formData()
    const file = form.get('file')
    // Not `instanceof File`: the entry's class differs between the browser worker
    // and the Node interceptor, so it is duck-typed instead.
    const filename = typeof file === 'string' || file === null ? '' : ((file as File).name ?? '')
    if (!filename) {
      return problem(422, 'Missing file', 'Upload a .scad file.')
    }
    if (!filename.endsWith('.scad')) {
      return problem(415, 'Unsupported file type', 'ScadBuddy accepts .scad source files.')
    }
    const slug = filename
      .replace(/\.scad$/, '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/(^-|-$)/g, '')
    const model: ModelSummary = {
      slug,
      name: slug.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
      description: 'Uploaded just now. Open it to see its parameters.',
      tags: ['uploaded'],
      updated_at: new Date().toISOString(),
      output_count: 0,
    }
    state.models = [model, ...state.models.filter((m) => m.slug !== slug)]
    state.schemas[slug] = fixtures.keychainSchema
    await delay(150)
    return HttpResponse.json(model, { status: 201 })
  }),

  http.get(`${base}/models/:slug`, ({ params }) => {
    const model = state.models.find((m) => m.slug === params['slug'])
    return model ? HttpResponse.json(model) : problem(404, 'Model not found')
  }),

  http.delete(`${base}/models/:slug`, ({ params }) => {
    state.models = state.models.filter((m) => m.slug !== params['slug'])
    return new HttpResponse(null, { status: 204 })
  }),

  http.get(`${base}/models/:slug/schema`, ({ params }) => {
    const schema = state.schemas[String(params['slug'])]
    return schema ? HttpResponse.json(schema) : problem(404, 'Model not found')
  }),

  http.post(`${base}/models/:slug/render`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const body = (await request.json()) as { params: Record<string, ParamValue> }
    const schema = state.schemas[slug]
    if (!schema) return problem(404, 'Model not found')

    const known = new Set(schema.groups.flatMap((g) => g.params).map((p) => p.name))
    const unknown = Object.keys(body.params).filter((key) => !known.has(key))
    if (unknown.length > 0) {
      return problem(422, 'Unknown parameter', `Not in the model schema: ${unknown.join(', ')}`)
    }

    const jobId = nextId('job')
    state.jobs.set(jobId, {
      job_id: jobId,
      slug,
      status: 'pending',
      created_at: new Date().toISOString(),
      params: body.params,
      polls: 0,
    })
    return HttpResponse.json({ job_id: jobId }, { status: 202 })
  }),

  http.get(`${base}/jobs/:id`, ({ params }) => {
    const job = state.jobs.get(String(params['id']))
    if (!job) return problem(404, 'Job not found')

    job.polls += 1
    if (job.polls === 1) {
      job.status = 'running'
      job.log_tail = 'Compiling design (CSG Tree generation)...'
      return HttpResponse.json(jobView(job))
    }

    if (String(job.params['name'] ?? '').toLowerCase() === fixtures.FAILING_NAME) {
      job.status = 'failed'
      job.log_tail = fixtures.OPENSCAD_LOG_TAIL
      return HttpResponse.json(jobView(job))
    }

    job.status = 'done'
    job.bbox_mm = bboxOf(job.params)
    job.colors = colorsOf(job.slug, job.params)
    job.preview_url = `${base}/jobs/${job.job_id}/preview.glb`
    job.log_tail = 'Geometries in cache: 12\nTotal rendering time: 0:00:00.412'
    return HttpResponse.json(jobView(job))
  }),

  http.get(`${base}/jobs/:id/preview.glb`, ({ params }) => {
    const job = state.jobs.get(String(params['id']))
    if (!job || !job.bbox_mm) return problem(404, 'Preview not ready')
    const glb = keychainGlb(job.colors ?? ['#9AA4B2'], job.bbox_mm)
    return HttpResponse.arrayBuffer(glb.buffer.slice(0) as ArrayBuffer, {
      headers: { 'Content-Type': 'model/gltf-binary' },
    })
  }),

  http.post(`${base}/models/:slug/outputs`, async ({ params, request }) => {
    const slug = String(params['slug'])
    const body = (await request.json()) as { job_id: string }
    const job = state.jobs.get(body.job_id)
    if (!job || job.status !== 'done') {
      return problem(409, 'Job not finished', 'Wait for the render to finish before generating.')
    }
    const output: Output = {
      id: nextId('out'),
      slug,
      created_at: new Date().toISOString(),
      params: job.params,
      bbox_mm: job.bbox_mm,
      colors: job.colors ?? [],
    }
    state.outputs = [output, ...state.outputs]
    state.models = state.models.map((m) =>
      m.slug === slug
        ? { ...m, output_count: m.output_count + 1, last_generated_at: output.created_at }
        : m,
    )
    await delay(120)
    return HttpResponse.json(output, { status: 201 })
  }),

  http.get(`${base}/models/:slug/outputs`, ({ params }) =>
    HttpResponse.json(state.outputs.filter((o) => o.slug === params['slug'])),
  ),

  http.delete(`${base}/outputs/:id`, ({ params }) => {
    state.outputs = state.outputs.filter((o) => o.id !== params['id'])
    return new HttpResponse(null, { status: 204 })
  }),

  http.get(`${base}/outputs/:id/model.3mf`, ({ params }) => {
    const output = state.outputs.find((o) => o.id === params['id'])
    if (!output) return problem(404, 'Output not found')
    // A 3MF is a zip; the mock serves a stub so the download path is exercised.
    return HttpResponse.arrayBuffer(new Uint8Array([0x50, 0x4b, 0x03, 0x04]).buffer, {
      headers: {
        'Content-Type': 'application/vnd.ms-package.3dmanufacturing-3dmodel+xml',
        'Content-Disposition': `attachment; filename="${output.slug}-${output.id}.3mf"`,
      },
    })
  }),

  http.put(`${base}/outputs/:id/thumbnail.png`, async ({ request }) => {
    await request.arrayBuffer()
    return new HttpResponse(null, { status: 204 })
  }),

  http.post(`${base}/outputs/:id/send`, async ({ params, request }) => {
    const id = String(params['id'])
    const body = (await request.json()) as { mode: 'library' | 'queue'; copies: number }
    const output = state.outputs.find((o) => o.id === id)
    if (!output) return problem(404, 'Output not found')
    if (!state.settings.api_key_set) {
      return problem(409, 'Bambuddy is not connected', 'Add an API key on the settings page.')
    }
    await delay(250)
    const libraryFileId = output.library_file_id ?? nextId('lib')
    const queueItemId = body.mode === 'queue' ? nextId('q') : undefined
    state.outputs = state.outputs.map((o) =>
      o.id === id ? { ...o, library_file_id: libraryFileId, queue_item_id: queueItemId } : o,
    )
    state.sent.set(id, { library_file_id: libraryFileId, queue_item_id: queueItemId })
    return HttpResponse.json({
      mode: body.mode,
      library_file_id: libraryFileId,
      queue_item_id: queueItemId,
      queue_url: queueItemId
        ? `${state.settings.bambuddy_url}/queue?item=${queueItemId}`
        : `${state.settings.bambuddy_url}/library/files/${libraryFileId}`,
    })
  }),

  http.get(`${base}/fonts`, () => HttpResponse.json(fixtures.fonts)),

  http.get(`${base}/settings`, () => HttpResponse.json(state.settings)),

  http.put(`${base}/settings`, async ({ request }) => {
    const body = (await request.json()) as {
      bambuddy_url: string
      api_key?: string
      library_folder_id?: string
      pipeline_id?: string
    }
    state.settings = {
      ...state.settings,
      bambuddy_url: body.bambuddy_url,
      library_folder_id: body.library_folder_id,
      pipeline_id: body.pipeline_id,
      api_key_set:
        body.api_key === undefined ? state.settings.api_key_set : body.api_key.length > 0,
    }
    await delay(120)
    return HttpResponse.json(state.settings)
  }),

  http.post(`${base}/settings/test`, async ({ request }) => {
    const body = (await request.json()) as { bambuddy_url: string; api_key?: string }
    await delay(200)
    if (!body.bambuddy_url.startsWith('http')) {
      return HttpResponse.json({ ok: false, detail: 'Enter a URL starting with http or https.' })
    }
    if (body.api_key !== undefined && body.api_key.length === 0 && !state.settings.api_key_set) {
      return HttpResponse.json({ ok: false, detail: 'No API key stored. Paste one and try again.' })
    }
    return HttpResponse.json({
      ok: true,
      detail: 'Connected. Manage Library, Manage Queue and Read Status are all granted.',
      printers: [
        { id: 'p1', name: 'X1C · Workshop' },
        { id: 'p2', name: 'A1 mini · Office' },
      ],
    })
  }),

  http.get(`${base}/settings/targets`, () => HttpResponse.json(fixtures.targets)),

  http.post(`${base}/settings/register-sidebar`, async () => {
    await delay(200)
    state.settings = { ...state.settings, sidebar_registered: true }
    return HttpResponse.json({
      ok: true,
      detail: 'ScadBuddy now appears in the Bambuddy sidebar as "Customize".',
    })
  }),
]
