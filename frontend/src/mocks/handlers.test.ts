import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ModelSummary } from '../api/types'

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

  it('keeps a whitespace description as given, and reads whitespace tags as none', async () => {
    const { body } = await upload(
      { description: '  ', tags: '  ' },
      { description: 'From JSON', tags: ['json'] },
    )
    expect(body.description).toBe('  ')
    expect(body.tags).toEqual([])
  })

  it('refuses tags that start as a JSON array but are not valid JSON', async () => {
    const { status, body } = await upload({ tags: '[not json' })
    expect(status).toBe(422)
    expect(body.detail).toBe('tags is not valid JSON')
  })
})
