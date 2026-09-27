import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from '../api/client'
import type { ModelSummary } from '../api/types'
import { BUILTIN_SLUG, versionIds } from './fixtures'

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
  value: string
  filename?: string
}

function multipart(parts: Part[]): string {
  return (
    parts
      .map(({ name, value, filename }) =>
        [
          `--${BOUNDARY}`,
          `Content-Disposition: form-data; name="${name}"${filename ? `; filename="${filename}"` : ''}`,
          ...(filename ? ['Content-Type: application/octet-stream'] : []),
          '',
          value,
        ].join('\r\n'),
      )
      .join('\r\n') + `\r\n--${BOUNDARY}--\r\n`
  )
}

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
  // Node's own `File`. Imported by a computed name because the app's tsconfig
  // (which covers these tests) deliberately carries no Node types.
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

  it('refuses tags that start as a JSON array but are not valid JSON', async () => {
    const { status, body } = await upload({ tags: '[not json' })
    expect(status).toBe(422)
    expect(body.detail).toBe('tags is not valid JSON')
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
