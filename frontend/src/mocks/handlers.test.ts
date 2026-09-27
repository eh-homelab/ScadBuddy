import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../api/client'
import type { ModelSummary } from '../api/types'
import { COPY, UPSTREAM, duplicateWithUpdate, ours, theirs } from '../test/upstream'
import { BUILTIN_SLUG, keychainSource, versionIds } from './fixtures'
import { MAX_PRESET_NAME, MAX_PRESETS, resetMockState, setMockPresets } from './handlers'

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
    expect(body).toMatchObject({ has_thumbnail: true, thumbnail_source: 'model' })
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
