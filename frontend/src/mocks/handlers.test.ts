import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../api/client'
import type { ModelPatch, ModelSummary, PrintRunRequest, Settings } from '../api/types'
import { DEFAULT_NOZZLES } from '../lib/printChoices'
import { COPY, UPSTREAM, duplicateWithUpdate, ours, theirs } from '../test/upstream'
import {
  BUILTIN_PREVIEW_ID,
  BUILTIN_SLUG,
  GALLERY_SLUG,
  MEDIA_MP4_BASE64,
  keychainSource,
  outputs,
  versionIds,
} from './fixtures'
import {
  MAX_PRESET_DESCRIPTION,
  MAX_PRESET_ID,
  MAX_PRESET_TAG,
  MAX_PRESET_TAGS,
  MAX_PRESET_NAME,
  MAX_PRESETS,
  resetMockState,
  setMockPresets,
  setMockUploadLimit,
} from './handlers'

/**
 * The mock's multipart `POST /models` has to resolve a model's name, description
 * and tags exactly as the backend's `create_model` does (#179), or the UI's tests
 * and the mocked e2e run pass against behaviour the real server does not have.
 *
 * The body is written by hand: jsdom's `File` and Node's fetch cannot agree on a
 * `FormData` body (see CataloguePage.test), but a multipart string is just text.
 *
 * Parsing it back has the same disagreement, the other way round. Node 24's undici
 * builds each file part with whatever `File` is global -- jsdom's, in this
 * environment -- and then asserts it is an instance of the `File` it captured when
 * it loaded, which is Node's own. Every request with a file part then dies in the
 * parser and the handler answers 500 (Node 22's undici does not check). So for
 * these requests the global `File` is Node's, which is what the browser worker and
 * the real server see anyway.
 */
const BOUNDARY = 'scadbuddy-test-boundary'

interface Part {
  name: string
  /** Bytes for a part that must not be re-encoded as UTF-8, such as a PNG. */
  value: string | Uint8Array
  filename?: string
}

function multipart(parts: Part[]): Uint8Array<ArrayBuffer> {
  const encoder = new TextEncoder()
  const chunks = parts.flatMap(({ name, value, filename }) => [
    encoder.encode(
      [
        `--${BOUNDARY}`,
        `Content-Disposition: form-data; name="${name}"${filename ? `; filename="${filename}"` : ''}`,
        ...(filename ? ['Content-Type: application/octet-stream'] : []),
        '',
        '',
      ].join('\r\n'),
    ),
    typeof value === 'string' ? encoder.encode(value) : value,
    encoder.encode('\r\n'),
  ])
  chunks.push(encoder.encode(`--${BOUNDARY}--\r\n`))
  const body = new Uint8Array(chunks.reduce((total, chunk) => total + chunk.length, 0))
  let offset = 0
  for (const chunk of chunks) {
    body.set(chunk, offset)
    offset += chunk.length
  }
  return body
}

/** Node's own `File` as the global for a block's requests with a file part (see above). */
function withNodeFile(): void {
  // Imported by a computed name because the app's tsconfig (which covers these
  // tests) deliberately carries no Node types.
  let NodeFile: typeof File
  beforeAll(async () => {
    const builtin = 'node:buffer'
    ;({ File: NodeFile } = (await import(/* @vite-ignore */ builtin)) as { File: typeof File })
  })
  beforeEach(() => {
    vi.stubGlobal('File', NodeFile)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })
}

const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]

/** A body that passes `_require_png`: the PNG signature, padded to `size` bytes. */
function png(size = 16): Uint8Array {
  const bytes = new Uint8Array(size)
  bytes.set(PNG_MAGIC)
  return bytes
}

const TOO_LARGE =
  'the thumbnail is too large: 10485761 bytes, and a thumbnail is at most 10485760 bytes (10 MiB)'

async function upload(
  fields: Record<string, string> = {},
  meta?: Record<string, unknown>,
): Promise<{ status: number; body: ModelSummary & { detail?: string } }> {
  const parts: Part[] = [{ name: 'file', value: 'cube(10);\n', filename: 'widget.scad' }]
  if (meta) parts.push({ name: 'meta', value: JSON.stringify(meta), filename: 'model.json' })
  for (const [name, value] of Object.entries(fields)) parts.push({ name, value })
  const response = await fetch('/api/v1/models', {
    method: 'POST',
    headers: { 'Content-Type': `multipart/form-data; boundary=${BOUNDARY}` },
    body: multipart(parts),
  })
  return { status: response.status, body: (await response.json()) as ModelSummary & { detail?: string } }
}

describe('mock POST /models (multipart), as the backend resolves details', () => {
  withNodeFile()

  it('names a model after its slug when nothing else names it', async () => {
    const { status, body } = await upload()
    expect(status).toBe(201)
    expect(body).toMatchObject({ slug: 'widget', name: 'widget', description: '', tags: [] })
  })

  it.each(['', '   ', '\t\n'])('treats a blank model.json name %j as absent', async (blank) => {
    const { body } = await upload({}, { name: blank })
    expect(body.name).toBe('widget')
  })

  it('treats null model.json fields as absent, as the backend does', async () => {
    const { body } = await upload({}, { name: null, description: null, tags: null })
    expect(body).toMatchObject({ name: 'widget', description: '', tags: [] })
  })

  it('takes the model.json name, stripped', async () => {
    const { body } = await upload({}, { name: '  Widget Deluxe  ' })
    expect(body.name).toBe('Widget Deluxe')
  })

  it('lets a form-field name win over the model.json name, stripped', async () => {
    const { body } = await upload({ name: '  From Form  ' }, { name: 'From JSON' })
    expect(body.name).toBe('From Form')
  })

  it('falls through a blank form-field name to the model.json name', async () => {
    const { body } = await upload({ name: '   ' }, { name: 'From JSON' })
    expect(body.name).toBe('From JSON')
  })

  it('lets form description and tags win over the model.json ones', async () => {
    const { body } = await upload(
      { description: 'From form', tags: 'a, b ,, c' },
      { description: 'From JSON', tags: ['json'] },
    )
    expect(body.description).toBe('From form')
    expect(body.tags).toEqual(['a', 'b', 'c'])
  })

  it('reads tags given as a JSON array', async () => {
    const { body } = await upload({ tags: '["x", "y"]' }, { tags: ['json'] })
    expect(body.tags).toEqual(['x', 'y'])
  })

  it('treats an empty description or tags field as absent, as FastAPI does', async () => {
    const { body } = await upload(
      { description: '', tags: '' },
      { description: 'From JSON', tags: ['json'] },
    )
    expect(body.description).toBe('From JSON')
    expect(body.tags).toEqual(['json'])
  })

  it('treats a whitespace-only description or tags field as absent too', async () => {
    const { body } = await upload(
      { description: '  ', tags: ' \t ' },
      { description: 'From JSON', tags: ['json'] },
    )
    expect(body.description).toBe('From JSON')
    expect(body.tags).toEqual(['json'])
  })

  it('keeps a non-blank form description exactly as given', async () => {
    const { body } = await upload({ description: '  From form  ' }, { description: 'From JSON' })
    expect(body.description).toBe('  From form  ')
  })

  it('lets an explicit empty tag list clear the model.json tags', async () => {
    const { body } = await upload({ tags: '[]' }, { tags: ['json'] })
    expect(body.tags).toEqual([])
  })

  it('gives the defaults for blank fields without a model.json', async () => {
    const { body } = await upload({ description: '   ', tags: '   ' })
    expect(body).toMatchObject({ description: '', tags: [] })
  })

  it.each([
    ['{not json', 'the model.json is not valid JSON'],
    ['[1, 2]', 'the model.json is not an object'],
    ['null', 'the model.json is not an object'],
  ])('refuses a model.json of %s as the backend does', async (meta, detail) => {
    const parts: Part[] = [
      { name: 'file', value: 'cube(10);\n', filename: 'widget.scad' },
      { name: 'meta', value: meta, filename: 'model.json' },
    ]
    const response = await fetch('/api/v1/models', {
      method: 'POST',
      headers: { 'Content-Type': `multipart/form-data; boundary=${BOUNDARY}` },
      body: multipart(parts),
    })
    expect(response.status).toBe(422)
    expect(((await response.json()) as { detail: string }).detail).toBe(detail)
  })

  it('refuses tags that start as a JSON array but are not valid JSON', async () => {
    const { status, body } = await upload({ tags: '[not json' })
    expect(status).toBe(422)
    expect(body.detail).toBe('tags is not valid JSON')
  })

  async function uploadSource(source: string) {
    const response = await fetch('/api/v1/models', {
      method: 'POST',
      headers: { 'Content-Type': `multipart/form-data; boundary=${BOUNDARY}` },
      body: multipart([{ name: 'file', value: source, filename: 'widget.scad' }]),
    })
    return { status: response.status, body: (await response.json()) as ModelSummary & { detail?: string } }
  }

  it('takes an uploaded source of exactly MAX_SOURCE_CHARS characters', async () => {
    const { status } = await uploadSource('\u{1F600}'.repeat(1_000_000))
    expect(status).toBe(201)
  })

  it('refuses an uploaded source one character over the cap, and creates nothing', async () => {
    const { status, body } = await uploadSource('x'.repeat(1_000_001))
    expect(status).toBe(422)
    expect(body.detail).toBe(
      'the source is too large: 1000001 characters, and this route reads at most 1000000',
    )
    expect((await api.listModels()).some((model) => model.slug === 'widget')).toBe(false)
  })

  /** A readable model.json of exactly `size` bytes; JSON allows trailing spaces. */
  function metaOf(size: number): string {
    const body = JSON.stringify({ name: 'Widget' })
    return body + ' '.repeat(size - body.length)
  }

  async function uploadMeta(meta: string) {
    const response = await fetch('/api/v1/models', {
      method: 'POST',
      headers: { 'Content-Type': `multipart/form-data; boundary=${BOUNDARY}` },
      body: multipart([
        { name: 'file', value: 'cube(10);\n', filename: 'widget.scad' },
        { name: 'meta', value: meta, filename: 'model.json' },
      ]),
    })
    return { status: response.status, body: (await response.json()) as ModelSummary & { detail?: string } }
  }

  it('takes a model.json of exactly MAX_META_BYTES', async () => {
    const { status, body } = await uploadMeta(metaOf(64 * 1024))
    expect(status).toBe(201)
    expect(body.name).toBe('Widget')
  })

  it('refuses a model.json one byte over the cap, as _read_meta_part does, and creates nothing', async () => {
    const { status, body } = await uploadMeta(metaOf(64 * 1024 + 1))
    expect(status).toBe(422)
    expect(body.detail).toBe(
      'the model.json is too large: 65537 bytes, and a model.json is at most 65536 bytes (64 KiB)',
    )
    expect((await api.listModels()).some((model) => model.slug === 'widget')).toBe(false)
  })

  async function uploadThumbnail(thumbnail: Uint8Array) {
    const response = await fetch('/api/v1/models', {
      method: 'POST',
      headers: { 'Content-Type': `multipart/form-data; boundary=${BOUNDARY}` },
      body: multipart([
        { name: 'file', value: 'cube(10);\n', filename: 'widget.scad' },
        { name: 'thumbnail', value: thumbnail, filename: 'thumbnail.png' },
      ]),
    })
    return { status: response.status, body: (await response.json()) as ModelSummary & { detail?: string } }
  }

  it('takes a thumbnail that is a PNG by its bytes', async () => {
    const { status, body } = await uploadThumbnail(png())
    expect(status).toBe(201)
    expect(body).toMatchObject({ has_thumbnail: true, thumbnail_source: 'model' })
  })

  it.each([
    ['is not a PNG by its bytes', new TextEncoder().encode('GIF89a'), 'the thumbnail is not a PNG'],
    ['is over the limit', png(10 * 1024 * 1024 + 1), TOO_LARGE],
  ])('refuses a thumbnail that %s, as _require_png does, and creates nothing', async (_, bytes, detail) => {
    const { status, body } = await uploadThumbnail(bytes)
    expect(status).toBe(422)
    expect(body.detail).toBe(detail)
    expect((await api.listModels()).some((model) => model.slug === 'widget')).toBe(false)
  })
})

describe('mock PUT /models/:slug/thumbnail, as _require_png holds it', () => {
  withNodeFile()

  async function put(bytes: Uint8Array) {
    const response = await fetch('/api/v1/models/name-keychain/thumbnail', {
      method: 'PUT',
      headers: { 'Content-Type': `multipart/form-data; boundary=${BOUNDARY}` },
      body: multipart([{ name: 'file', value: bytes, filename: 'cover.png' }]),
    })
    return { status: response.status, body: (await response.json()) as ModelSummary & { detail?: string } }
  }

  it('sets a PNG within the limit', async () => {
    const { status, body } = await put(png())
    expect(status).toBe(200)
    expect(body).toMatchObject({
      has_thumbnail: true,
      thumbnail_source: 'model',
      thumbnail_preview_id: null,
    })
  })

  it.each([
    ['is not a PNG by its bytes', new TextEncoder().encode('GIF89a'), 'the thumbnail is not a PNG'],
    ['is over the limit', png(10 * 1024 * 1024 + 1), TOO_LARGE],
  ])('refuses one that %s with the backend\'s 422, and changes nothing', async (_, bytes, detail) => {
    const before = await api.getModel('name-keychain')
    const { status, body } = await put(bytes)
    expect(status).toBe(422)
    expect(body.detail).toBe(detail)
    expect(await api.getModel('name-keychain')).toEqual(before)
  })
})

const refusal = `'${BUILTIN_SLUG}' is a built-in template and is read-only`

describe('mock API: a built-in template (#192)', () => {
  it.each([
    ['delete', () => api.deleteModel(BUILTIN_SLUG)],
    ['replace the source of', () => api.replaceSource(BUILTIN_SLUG, 'cube(1);\n')],
    ['restore a revision of', () => api.restoreVersion(BUILTIN_SLUG, versionIds.builtinFirst)],
  ])('refuses to %s one with the 403 require_mine answers', async (_, write) => {
    const error: unknown = await write().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 403, detail: refusal })
  })

  it('refuses a metadata edit the same way', async () => {
    const response = await fetch(`/api/v1/models/${encodeURIComponent(BUILTIN_SLUG)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Mine now' }),
    })
    expect(response.status).toBe(403)
    expect(response.headers.get('Content-Type')).toBe('application/problem+json')
    expect(await response.json()).toMatchObject({ status: 403, detail: refusal })
  })

  // #179's details writes, refused the same way, before anything is read or written.
  it.each([
    ['remove the thumbnail of', () => api.removeThumbnail(BUILTIN_SLUG)],
    ['set the README of', () => api.setReadme(BUILTIN_SLUG, '# Mine now\n')],
    ['remove the README of', () => api.removeReadme(BUILTIN_SLUG)],
  ])('refuses to %s one with the same 403', async (_, write) => {
    const error: unknown = await write().catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 403, detail: refusal })
  })

  it('refuses a new thumbnail the same way', async () => {
    // A bare body: the refusal comes before the upload is read.
    const response = await fetch(`/api/v1/models/${encodeURIComponent(BUILTIN_SLUG)}/thumbnail`, {
      method: 'PUT',
      headers: { 'Content-Type': 'image/png' },
      body: 'png',
    })
    expect(response.status).toBe(403)
    expect(await response.json()).toMatchObject({ status: 403, detail: refusal })
  })

  it('gives a new model with no thumbnail its preview on a later read, as the backend does', async () => {
    const created = await api.createModelFromSource({
      name: 'Fresh Widget',
      source: 'cube(1);\n',
      description: '',
      force: false,
    })
    // The render runs in the background: the create answers before it.
    expect(created).toMatchObject({ has_thumbnail: false })
    expect(created.thumbnail_source ?? null).toBeNull()

    const read = await api.getModel(created.slug)
    expect(read).toMatchObject({ has_thumbnail: true, thumbnail_source: 'preview' })
    expect(read.thumbnail_preview_id).toMatch(/^[0-9a-f]{16}$/)
    const listed = (await api.listModels()).find((model) => model.slug === created.slug)
    expect(listed?.thumbnail_preview_id).toBe(read.thumbnail_preview_id)

    // Each model's render is its own.
    const other = await api.createModelFromSource({
      name: 'Other Widget',
      source: 'cube(2);\n',
      description: '',
      force: false,
    })
    expect((await api.getModel(other.slug)).thumbnail_preview_id).not.toBe(read.thumbnail_preview_id)
  })

  it('lists its default-render preview, having no thumbnail of its own', async () => {
    const builtin = await api.getModel(BUILTIN_SLUG)
    expect(builtin).toMatchObject({
      has_thumbnail: true,
      thumbnail_source: 'preview',
      thumbnail_output_id: null,
      thumbnail_preview_id: BUILTIN_PREVIEW_ID,
    })
    expect(api.modelThumbnailUrl(builtin)).toContain(BUILTIN_PREVIEW_ID)
  })

  it('does not hand its preview to a duplicate, which is rendered afresh', async () => {
    const copy = await api.duplicateModel(BUILTIN_SLUG, 'My Keychain')
    expect(copy).toMatchObject({
      has_thumbnail: false,
      thumbnail_source: null,
      thumbnail_preview_id: null,
    })
  })

  it('still serves every read', async () => {
    expect((await api.getModel(BUILTIN_SLUG)).origin).toBe('builtin')
    expect(await api.listVersions(BUILTIN_SLUG)).toHaveLength(2)
    expect((await api.getSchema(BUILTIN_SLUG)).title).toBe('Keychain Template')
    expect(await api.getSource(BUILTIN_SLUG)).toContain('/* [Text] */')
  })
})

describe('mock API: duplicate (#156)', () => {
  it('answers 201 with a template of mine recording its upstream', async () => {
    const response = await fetch(`/api/v1/models/${encodeURIComponent(BUILTIN_SLUG)}/duplicate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'My Keychain' }),
    })
    expect(response.status).toBe(201)
    expect(await response.json()).toMatchObject({
      slug: 'my-keychain',
      name: 'My Keychain',
      origin: 'mine',
      upstream: { id: BUILTIN_SLUG, path: '_builtin/keychain-template', base: versionIds.synced },
    })
    expect(await api.getSource('my-keychain')).toContain('/* [Text] */')
    expect((await api.listVersions('my-keychain'))[0]?.message).toBe(
      `Duplicate ${BUILTIN_SLUG} as my-keychain`,
    )
  })

  it('refuses a slug that is taken with a 409', async () => {
    const error: unknown = await api
      .duplicateModel(BUILTIN_SLUG, 'Name Keychain')
      .catch((caught: unknown) => caught)
    expect(error).toMatchObject({
      status: 409,
      detail: "a model named 'name-keychain' already exists",
    })
  })

  it('answers 404 for a template that is not there', async () => {
    const error: unknown = await api
      .duplicateModel('gone', 'Copy')
      .catch((caught: unknown) => caught)
    expect(error).toMatchObject({ status: 404 })
  })
})

describe('mock API: a duplicate takes its upstream\'s thumbnail and README (#179)', () => {
  it('copies them as its own, but not the plate fallback, which is derived', async () => {
    const copy = await fetch('/api/v1/models/name-keychain/duplicate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Keychain copy' }),
    }).then((response) => response.json() as Promise<ModelSummary>)

    expect(copy).toMatchObject({
      origin: 'mine',
      has_thumbnail: true,
      thumbnail_source: 'model',
      thumbnail_output_id: null,
      has_readme: true,
    })
    expect(await api.getReadme(copy.slug)).toBe(await api.getReadme('name-keychain'))
  })
})

describe('mock API: upstream updates (#157)', () => {
  it('reports a duplicate current, then an update once its upstream moves', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    expect((await api.getUpstream(COPY)).state).toBe('current')
    expect((await api.getModel(COPY)).upstream_state).toBe('current')

    await api.replaceSource(UPSTREAM, theirs)
    expect(await api.getUpstream(COPY)).toMatchObject({
      state: 'update',
      revision: (await api.listVersions(UPSTREAM))[0]?.commit,
      preview: { ours: keychainSource, base: keychainSource, theirs, merged: theirs, clean: true },
    })
    expect((await api.listModels()).find((m) => m.slug === COPY)?.upstream_state).toBe('update')
  })

  it('serves upstream_state on a metadata edit, as on every other write', async () => {
    await duplicateWithUpdate()
    expect((await api.updateModel(COPY, { description: 'Mine' })).upstream_state).toBe('update')
  })

  it('answers 404 for a template that is not a duplicate', async () => {
    const error: unknown = await api.getUpstream(UPSTREAM).catch((caught: unknown) => caught)
    expect(error).toMatchObject({
      status: 404,
      detail: `'${UPSTREAM}' is not a duplicate, so it has no upstream`,
    })
  })

  it('answers a conflicted merge with 409, the marked-up source and merge_base', async () => {
    await duplicateWithUpdate({ conflict: true })
    const revision = (await api.listVersions(UPSTREAM))[0]?.commit
    const error: unknown = await api.mergeUpstream(COPY).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    const { problem } = error as ApiError
    expect(problem).toMatchObject({ status: 409, merge_base: revision, conflicts: 1 })
    expect(problem['merged']).toContain('<<<<<<< ')
    expect(await api.getSource(COPY)).toBe(ours)
  })

  it('refuses a resolution that still has markers, or names no upstream revision', async () => {
    await duplicateWithUpdate({ conflict: true })
    const revision = (await api.listVersions(UPSTREAM))[0]?.commit ?? ''
    const marked: unknown = await api
      .resolveUpstreamMerge(COPY, '<<<<<<< ours\ncube(1);\n', revision)
      .catch((caught: unknown) => caught)
    expect(marked).toMatchObject({ status: 422 })
    const stranger: unknown = await api
      .resolveUpstreamMerge(COPY, 'cube(1);\n', versionIds.synced)
      .catch((caught: unknown) => caught)
    expect(stranger).toMatchObject({ status: 422 })

    await api.resolveUpstreamMerge(COPY, 'cube(1);\n', revision)
    expect((await api.getModel(COPY)).upstream_state).toBe('current')
  })

  it('clears dismissed when a conflicted merge is resolved, as a clean one does', async () => {
    await duplicateWithUpdate({ conflict: true })
    await api.dismissUpstream(COPY)
    await api.replaceSource(UPSTREAM, `${theirs}// again\n`)
    const revision = (await api.listVersions(UPSTREAM))[0]?.commit ?? ''

    const resolved = await api.resolveUpstreamMerge(COPY, 'cube(1);\n', revision)
    expect(resolved.upstream).toEqual({
      id: UPSTREAM,
      path: UPSTREAM,
      base: revision,
      dismissed: null,
    })
    expect(resolved.upstream_state).toBe('current')
  })

  it('clears dismissed on a clean merge of a dismissed update', async () => {
    await duplicateWithUpdate()
    await api.dismissUpstream(COPY)
    const revision = (await api.listVersions(UPSTREAM))[0]?.commit
    const merged = await api.mergeUpstream(COPY)
    expect(merged.model.upstream).toMatchObject({ base: revision, dismissed: null })
    expect(merged.model.upstream_state).toBe('current')
  })

  it('dismisses an update until the upstream moves again', async () => {
    await duplicateWithUpdate()
    expect((await api.dismissUpstream(COPY)).upstream_state).toBe('dismissed')
    // #235: still previewed, as the merge it would still make.
    const status = await api.getUpstream(COPY)
    expect(status.state).toBe('dismissed')
    expect(status.preview?.merged).toBe(theirs)
    await api.replaceSource(UPSTREAM, `${theirs}// again\n`)
    expect((await api.getModel(COPY)).upstream_state).toBe('update')
  })

  it('refuses to dismiss with no update, and to detach while the upstream exists', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    for (const action of [() => api.dismissUpstream(COPY), () => api.detachUpstream(COPY)]) {
      const error: unknown = await action().catch((caught: unknown) => caught)
      expect(error).toMatchObject({ status: 409 })
    }
  })

  it('reports a deleted upstream gone, and detaches from it', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    await api.deleteModel(UPSTREAM, true)
    expect((await api.getUpstream(COPY)).state).toBe('gone')
    expect((await api.detachUpstream(COPY)).upstream).toBeNull()
  })
})

describe('mock API: delete a template duplicates track (#223)', () => {
  it('answers 409 naming them, and deletes with force', async () => {
    await api.duplicateModel('name-keychain', 'My Keychain')

    const error: unknown = await api.deleteModel('name-keychain').catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 409, problem: { duplicates: 1, slugs: ['my-keychain'] } })
    expect((await api.getModel('name-keychain')).slug).toBe('name-keychain')

    await api.deleteModel('name-keychain', true)
    const gone: unknown = await api.getModel('name-keychain').catch((caught: unknown) => caught)
    expect(gone).toMatchObject({ status: 404 })
  })
})

describe('mock API: a preset\'s description and tags (#327)', () => {
  beforeEach(() => resetMockState())

  it('cleans tags and trims the description as the server does', async () => {
    const created = await api.createPreset('name-keychain', {
      name: 'Bag tag',
      params: {},
      description: '  For bags. ',
      tags: [' big ', 'Big', '', 'kids  size'],
    })
    expect(created.description).toBe('For bags.')
    expect(created.tags).toEqual(['big', 'kids size'])
  })

  it('refuses details past their bounds as a body shape (422), before the route', async () => {
    const refused = (body: object) =>
      expect(
        api.createPreset('no-such-model', { name: 'X', params: {}, ...body }),
      ).rejects.toMatchObject({
        status: 422,
        detail: 'the request did not match the expected shape',
      })
    await refused({ description: 'd'.repeat(MAX_PRESET_DESCRIPTION + 1) })
    await refused({ tags: Array.from({ length: MAX_PRESET_TAGS + 1 }, (_, n) => `t${n}`) })
    await refused({ tags: ['t'.repeat(MAX_PRESET_TAG + 1)] })
    // A comma would split the tag in two in the Edit details dialog.
    await refused({ tags: ['M3, M4'] })
    // Lengths are code points, as Python counts them, and case folds as `casefold`.
    const wide = await api.createPreset('name-keychain', {
      name: 'Emoji',
      params: {},
      description: '\u{1F600}'.repeat(MAX_PRESET_DESCRIPTION),
      tags: ['\u{1F600}'.repeat(MAX_PRESET_TAG), 'Straße', 'STRASSE'],
    })
    expect(wide.description).toBe('\u{1F600}'.repeat(MAX_PRESET_DESCRIPTION))
    expect(wide.tags).toEqual(['\u{1F600}'.repeat(MAX_PRESET_TAG), 'Straße'])
    // Repeats are dropped before the bound: this many copies of one tag is one tag.
    const created = await api.createPreset('name-keychain', {
      name: 'Many',
      params: {},
      tags: Array.from({ length: MAX_PRESET_TAGS * 2 }, () => 'same'),
    })
    expect(created.tags).toEqual(['same'])
  })

  it('edits and clears a saved preset\'s details, keeping what is left out', async () => {
    const saved = await api.createPreset('name-keychain', { name: 'P', params: {}, tags: ['a'] })
    const edited = await api.updatePreset('name-keychain', saved.id, { description: 'D' })
    expect([edited.description, edited.tags]).toEqual(['D', ['a']])
    const cleared = await api.updatePreset('name-keychain', saved.id, { description: '', tags: [] })
    expect([cleared.description, cleared.tags]).toEqual(['', []])
  })

  it('copies the description and tags to a duplicate', async () => {
    const copy = await api.duplicatePreset('name-keychain', 'template-tiny', { name: 'Tiny 2' })
    expect(copy.description).toContain('zip pull')
    expect(copy.tags).toEqual(['small', 'zip pull'])
  })

  it('keeps a template\'s own details from its metadata', async () => {
    await api.updateModel('name-keychain', {
      presets: [{ id: 'wide', name: 'Wide', description: ' Wide. ', tags: ['w', 'W', ' x '] }],
    })
    const [wide] = await api.listPresets('name-keychain')
    expect([wide?.description, wide?.tags]).toEqual(['Wide.', ['w', 'x']])
  })
})

describe('mock API: presets keep the server limits', () => {
  beforeEach(() => resetMockState())

  it('refuses a name longer than the server takes', async () => {
    const long = 'x'.repeat(MAX_PRESET_NAME + 1)
    await expect(api.createPreset('name-keychain', { name: long, params: {} })).rejects.toMatchObject(
      { status: 422 },
    )
    const saved = await api.createPreset('name-keychain', {
      name: 'x'.repeat(MAX_PRESET_NAME),
      params: {},
    })
    await expect(
      api.updatePreset('name-keychain', saved.id, { name: long }),
    ).rejects.toMatchObject({ status: 422 })
  })

  it('refuses a new preset once a template keeps as many as the server allows', async () => {
    const existing = Array.from({ length: MAX_PRESETS }, (_, index) => ({
      id: `${index}`.padStart(32, '0'),
      name: `Preset ${index}`,
      origin: 'mine' as const,
      params: {},
      description: '',
      tags: [],
    }))
    setMockPresets('name-keychain', existing)
    const refused = api.createPreset('name-keychain', { name: 'One too many', params: {} })
    await expect(refused).rejects.toBeInstanceOf(ApiError)
    await expect(refused).rejects.toMatchObject({ status: 409 })
    // A bad value is refused first, as the server validates it before counting.
    await expect(
      api.createPreset('name-keychain', { name: 'Bad', params: { nope: 1 } }),
    ).rejects.toMatchObject({ status: 422 })
    // Editing one that is already there is still fine.
    const first = existing[0]!
    await expect(
      api.updatePreset('name-keychain', first.id, { name: 'Renamed' }),
    ).resolves.toMatchObject({ name: 'Renamed' })
  })
})

describe('mock API: preset values are checked as the server checks them', () => {
  beforeEach(() => resetMockState())

  it('refuses a value of the wrong type for a known parameter', async () => {
    await expect(
      api.createPreset('name-keychain', { name: 'Bad', params: { text_size: 'big' } }),
    ).rejects.toMatchObject({ status: 422 })
    await expect(
      api.createPreset('name-keychain', { name: 'Bad', params: { keyring_hole: 'yes' } }),
    ).rejects.toMatchObject({ status: 422 })
  })

  it('refuses a dropdown value that is not one of its options', async () => {
    await expect(
      api.createPreset('name-keychain', { name: 'Bad', params: { hole_side: 'bottom' } }),
    ).rejects.toMatchObject({ status: 422 })
    await expect(
      api.createPreset('name-keychain', { name: 'Good', params: { hole_side: 'top' } }),
    ).resolves.toMatchObject({ params: { hole_side: 'top' } })
  })
})

describe('mock API: duplicating a preset', () => {
  beforeEach(() => resetMockState())

  it('copies a shipped preset to a saved one with its values', async () => {
    const copy = await api.duplicatePreset('name-keychain', 'template-tiny', { name: 'Tiny copy' })
    expect(copy).toMatchObject({ origin: 'mine', params: { text_size: 10, keyring_hole: false } })
  })

  it('refuses a taken name, and a preset whose values the template no longer takes', async () => {
    await expect(
      api.duplicatePreset('name-keychain', 'template-tiny', { name: 'mum' }),
    ).rejects.toMatchObject({ status: 409 })
    // "Old engraving" names engrave_depth, which the schema has dropped.
    await expect(
      api.duplicatePreset('name-keychain', 'b1b2c3d4e5f60718293a4b5c6d7e8f90', { name: 'Copy' }),
    ).rejects.toMatchObject({ status: 422 })
  })
})

describe('mock media routes, as api/media.py holds them (#274)', () => {
  withNodeFile()
  beforeEach(() => resetMockState())

  const PNG = png()
  const MP4 = Uint8Array.from(atob(MEDIA_MP4_BASE64), (c) => c.charCodeAt(0))

  async function post(slug: string, parts: Part[]) {
    const response = await fetch(`/api/v1/models/${encodeURIComponent(slug)}/media`, {
      method: 'POST',
      headers: { 'Content-Type': `multipart/form-data; boundary=${BOUNDARY}` },
      body: multipart(parts),
    })
    return { status: response.status, body: (await response.json()) as ModelSummary & { detail?: string } }
  }

  it('lists a legacy thumbnail as one image, several items, and none', async () => {
    const models = await api.listModels()
    const of = (slug: string) => models.find((model) => model.slug === slug)?.media
    expect(of('name-keychain')).toEqual([expect.objectContaining({ id: 'thumbnail', kind: 'image' })])
    expect(of(GALLERY_SLUG)?.map((item) => item.kind)).toEqual(['image', 'image', 'image', 'video'])
    expect(of('gridfinity-bin')).toEqual([])
    expect(of(BUILTIN_SLUG)).toEqual([])
  })

  it('adds an image last, converting a legacy thumbnail into an ordinary item', async () => {
    const { status, body } = await post('name-keychain', [
      { name: 'file', value: PNG, filename: 'side.png' },
      { name: 'caption', value: 'The side' },
    ])
    expect(status).toBe(200)
    expect(body.media).toHaveLength(2)
    expect(body.media?.[0]?.id).toMatch(/^[0-9a-f]{12}$/)
    expect(body.media?.[1]).toMatchObject({ kind: 'image', caption: 'The side', content_type: 'image/png' })
    expect(body).toMatchObject({ has_thumbnail: true, thumbnail_source: 'model' })
  })

  it('adds a video with its poster, typed by its bytes', async () => {
    const { status, body } = await post('gridfinity-bin', [
      { name: 'file', value: MP4, filename: 'clip.bin' },
      { name: 'poster', value: PNG, filename: 'poster.png' },
    ])
    expect(status).toBe(200)
    const [item] = body.media ?? []
    expect(item).toMatchObject({ kind: 'video', content_type: 'video/mp4', poster: `${item?.id}-poster.png` })
    // A video with a poster is a cover.
    expect(body).toMatchObject({ has_thumbnail: true, thumbnail_source: 'model' })
  })

  it('refuses what is not media with a 415', async () => {
    const { status, body } = await post('gridfinity-bin', [
      { name: 'file', value: 'hello', filename: 'notes.png' },
    ])
    expect(status).toBe(415)
    expect(body.detail).toBe('the upload is not a PNG, JPEG or WebP image, or an MP4 or WebM video')
  })

  it('refuses every write to a built-in with a 403', async () => {
    // Refused before the item is looked up, as `require_mine` does.
    const id = 'a1b2c3d4e5f6'
    expect((await post(BUILTIN_SLUG, [{ name: 'file', value: PNG, filename: 'a.png' }])).status).toBe(403)
    await expect(api.patchMedia(BUILTIN_SLUG, id, 'x')).rejects.toMatchObject({ status: 403 })
    await expect(api.reorderMedia(BUILTIN_SLUG, [id])).rejects.toMatchObject({ status: 403 })
    await expect(api.deleteMedia(BUILTIN_SLUG, id)).rejects.toMatchObject({ status: 403 })
  })

  it('refuses an order that is not a permutation with a 422', async () => {
    const copy = await api.duplicateModel(GALLERY_SLUG, 'Gallery')
    const ids = (copy.media ?? []).map((item) => item.id)
    for (const order of [ids.slice(1), [...ids, ids[0]!], [...ids.slice(1), 'ffffffffffff']]) {
      await expect(api.reorderMedia(copy.slug, order)).rejects.toMatchObject({ status: 422 })
    }
  })

  it('answers 404 for an item the template does not have', async () => {
    await expect(api.deleteMedia('gridfinity-bin', 'ffffffffffff')).rejects.toMatchObject({ status: 404 })
    expect((await fetch('/api/v1/models/gridfinity-bin/media/ffffffffffff')).status).toBe(404)
  })

  it('serves an item and a poster with their types', async () => {
    const video = (await api.getModel(GALLERY_SLUG)).media?.find((item) => item.kind === 'video')
    const slug = encodeURIComponent(GALLERY_SLUG)
    const file = await fetch(`/api/v1/models/${slug}/media/${video!.id}`)
    expect(file.headers.get('Content-Type')).toBe('video/mp4')
    expect(new Uint8Array(await file.arrayBuffer())).toEqual(MP4)
    const poster = await fetch(`/api/v1/models/${slug}/media/${video!.id}/poster`)
    expect(poster.headers.get('Content-Type')).toBe('image/png')
  })

  it('drops the cover with the last image', async () => {
    const removed = await api.deleteMedia('name-keychain', 'thumbnail')
    expect(removed.media).toEqual([])
    expect(removed.thumbnail_source).not.toBe('model')
  })

  it('stores an upload limit a settings PUT sets, as a value set here (#322)', async () => {
    const saved = await fetch('/api/v1/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ media_upload_max_bytes: 1024 }),
    }).then((response) => response.json() as Promise<Settings>)
    expect(saved.media_upload_max_bytes).toBe(1024)
    expect(saved.sources?.media_upload_max_bytes).toBe('stored')
    const reset = await api.putSettings({ reset: ['media_upload_max_bytes'] })
    expect(reset.media_upload_max_bytes).toBe(1024 * 1024 * 1024)
    expect(reset.sources?.media_upload_max_bytes).toBe('default')
  })

  it('refuses an upload over the limit with a 413 naming it', async () => {
    const saved = (await api.getSettings()).media_upload_max_bytes
    try {
      setMockUploadLimit(1024 * 1024)
      const { status, body } = await post('gridfinity-bin', [
        { name: 'file', value: new Uint8Array([...MP4, ...new Uint8Array(1024 * 1024)]), filename: 'v.mp4' },
      ])
      expect(status).toBe(413)
      expect(body.detail).toBe(
        'a media upload is at most 1 MB (SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES), and this one is larger',
      )
    } finally {
      setMockUploadLimit(saved)
    }
  })

  it('copies the media with a duplicate', async () => {
    const copy = await api.duplicateModel(GALLERY_SLUG, 'Gallery')
    expect(copy.media).toEqual((await api.getModel(GALLERY_SLUG)).media)
  })
})

describe('mock API: a template of mine defines its presets in its metadata (#326)', () => {
  beforeEach(() => resetMockState())

  it('replaces the template presets and keeps the saved ones', async () => {
    await api.updateModel('name-keychain', {
      presets: [{ id: 'wide', name: 'Wide', params: { text_size: 20 } }, { name: 'Bag tag' }],
    })
    const presets = await api.listPresets('name-keychain')
    expect(presets.filter((p) => p.origin === 'template').map((p) => p.id)).toEqual([
      'template-wide',
      'template-bag-tag',
    ])
    expect(presets.filter((p) => p.origin === 'mine').map((p) => p.name)).toEqual([
      'Mum',
      'Old engraving',
    ])
  })

  it('refuses an unknown parameter and a repeated name', async () => {
    await expect(
      api.updateModel('name-keychain', { presets: [{ name: 'X', params: { nope: 1 } }] }),
    ).rejects.toMatchObject({ status: 422 })
    await expect(
      api.updateModel('name-keychain', { presets: [{ name: 'X' }, { name: 'x' }] }),
    ).rejects.toMatchObject({ status: 422 })
  })

  it('refuses a name a saved preset already has (409), and writes nothing', async () => {
    const before = await api.listPresets('name-keychain')
    await expect(
      api.updateModel('name-keychain', { presets: [{ name: 'mum' }] }),
    ).rejects.toMatchObject({ status: 409 })
    expect(await api.listPresets('name-keychain')).toEqual(before)
  })

  it('keys presets without an id as the server does: suffixed on a clash, by position without a slug', async () => {
    await api.updateModel('name-keychain', {
      presets: [
        { name: 'Bag tag' },
        { id: 'bag-tag-2', name: 'Other' },
        { name: 'Bag  Tag!' },
        { name: '🎄' },
      ],
    })
    const presets = await api.listPresets('name-keychain')
    expect(presets.filter((p) => p.origin === 'template').map((p) => p.id)).toEqual([
      'template-bag-tag',
      'template-bag-tag-2',
      'template-bag-tag-3',
      'template-preset-4',
    ])
  })

  it('refuses what the server refuses: a stale dropdown value, a wrong type, a repeated id, too many', async () => {
    const refused = (presets: ModelPatch['presets']) =>
      expect(api.updateModel('name-keychain', { presets })).rejects.toMatchObject({ status: 422 })
    await refused([{ name: 'X', params: { hole_side: 'bottom' } }])
    await refused([{ name: 'X', params: { text_size: 'big' } }])
    await refused([
      { id: 'same', name: 'One' },
      { id: 'same', name: 'Two' },
    ])
    await refused(Array.from({ length: MAX_PRESETS + 1 }, (_, n) => ({ name: `Preset ${n}` })))
    await refused([{ id: 'Not A Slug', name: 'X' }])
    await refused([{ id: 'a'.repeat(MAX_PRESET_ID + 1), name: 'X' }])
    await refused([{ name: 'X', description: 'd'.repeat(MAX_PRESET_DESCRIPTION + 1) }])
    await refused([{ name: 'X', tags: Array.from({ length: MAX_PRESET_TAGS + 1 }, (_, n) => `t${n}`) }])
    await refused([{ name: 'X', tags: ['t'.repeat(MAX_PRESET_TAG + 1)] }])
    await refused([{ name: '   ' }])
    await refused([{ name: 'x'.repeat(MAX_PRESET_NAME + 1) }])
    // Over-long before blank, as the server checks them: 81 spaces is a length problem.
    await expect(
      api.updateModel('name-keychain', { presets: [{ name: ' '.repeat(MAX_PRESET_NAME + 1) }] }),
    ).rejects.toMatchObject({
      detail: 'the request did not match the expected shape',
      problem: { errors: [expect.objectContaining({ msg: expect.stringContaining('at most') })] },
    })
    // The list's shape before any value: a repeated name after an unknown parameter is
    // still the name the server reports.
    await expect(
      api.updateModel('name-keychain', {
        presets: [{ name: 'A', params: { nope: 1 } }, { name: 'B' }, { name: 'b' }],
      }),
    ).rejects.toMatchObject({
      detail: 'the request did not match the expected shape',
      problem: { errors: [expect.objectContaining({ msg: expect.stringContaining('two presets are named') })] },
    })
    // Nothing was written by any of them.
    const presets = await api.listPresets('name-keychain')
    expect(presets.filter((p) => p.origin === 'template').map((p) => p.id)).toEqual([
      'template-tiny',
    ])
  })
})

describe('mock API: metadata PATCH on a model that is not there', () => {
  beforeEach(() => resetMockState())

  it('is a 404 before the presets\u2019 values are looked at, and writes nothing', async () => {
    await expect(
      api.updateModel('no-such-model', { presets: [{ name: 'X', params: { nope: 1 } }] }),
    ).rejects.toMatchObject({ status: 404 })
    await expect(api.listPresets('no-such-model')).rejects.toMatchObject({ status: 404 })
  })

  it('refuses a malformed list (422) before the route can answer 404 or 403', async () => {
    const twice = { presets: [{ name: 'X' }, { name: 'x' }] }
    await expect(api.updateModel('no-such-model', twice)).rejects.toMatchObject({ status: 422 })
    await expect(api.updateModel(BUILTIN_SLUG, twice)).rejects.toMatchObject({ status: 422 })
  })
})

describe('mock API: analyzer decisions', () => {
  beforeEach(() => resetMockState())

  it('refuses a suppression without a reason as FastAPI refuses a body it cannot parse', async () => {
    // `DecisionCreate._well_formed` is a model validator: `_validation_error` answers with
    // one detail for every such refusal and the message under `errors`, never in `detail`.
    const blank = {
      diagnostic_id: 'SB1002',
      kind: 'suppress' as const,
      scope: { kind: 'global' as const, key: '' },
      enforced: false,
      confirm: false,
    }
    await expect(api.createDecision({ ...blank, reason: '  ' })).rejects.toMatchObject({
      status: 422,
      detail: 'the request did not match the expected shape',
      problem: {
        errors: [{ loc: ['body'], msg: expect.stringContaining('a suppression needs a reason') }],
      },
    })
    const report = await api.runAnalyzers({
      target: { output_id: outputs[0]!.id },
      request: { plate_id: 1, all_plates: false },
      detail: 'advanced',
    })
    expect(report.diagnostics.map((row) => row.status)).not.toContain('suppressed')
  })
})

describe('library print', () => {
  beforeEach(() => resetMockState())

  const runBody: PrintRunRequest = {
    printer_id: 1,
    filament_plan: { slots: [], force_colour_match: false },
    choices: {
      nozzles: DEFAULT_NOZZLES,
      tier: 'standard',
      process_name: null,
      bed_type: 'Textured PEI Plate',
      filament_overrides: {},
    },
    plate_id: 1,
    all_plates: false,
  }

  it('lists the root 3MFs, and every file under all', async () => {
    const plain = await api.listLibrary({ folderId: null, all: false })
    const every = await api.listLibrary({ folderId: null, all: true })
    expect(plain.files?.map((file) => file.id)).toEqual([89])
    expect(every.files?.find((file) => file.id === 104)?.printable).toBe(false)
    expect(plain.hidden).toBe(1)
  })

  it('remembers the choices per file', async () => {
    await api.putLibraryChoices(89, {
      printer_id: 1,
      filament_plan: [],
      nozzles: [
        { size: '0.2', flow: 'standard' },
        { size: '0.2', flow: 'standard' },
      ],
    })
    expect((await api.getLibraryChoices(89)).model_choices?.nozzles?.[0]?.size).toBe('0.2')
    expect((await api.getLibraryChoices(67)).model_choices?.nozzles ?? []).toEqual([])
  })

  it('forgets the choices with every other remembered choice', async () => {
    await api.putLibraryChoices(89, { printer_id: 1, filament_plan: [] })
    await api.forgetAllRemembered()
    expect((await api.getLibraryChoices(89)).model_choices?.printer_id ?? null).toBeNull()
  })

  it('refuses a sliced file and a missing one', async () => {
    await expect(api.runLibraryPrint(104, runBody)).rejects.toMatchObject({ status: 422 })
    await expect(api.runLibraryPrint(999, runBody)).rejects.toMatchObject({ status: 404 })
  })
})
