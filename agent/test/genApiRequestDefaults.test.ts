// scripts/gen-api-request-defaults.mjs, the request-body defaults both gen-api scripts
// use (#6602, #6660, #6661), run through openapi-typescript on a hand-written spec.
import openapiTS, { astToString, type OpenAPITSOptions } from 'openapi-typescript'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

/** The module's exports, as the two gen-api scripts call them (#6670). */
type Shared = {
  requestDefaultsHook: (
    doc: object,
    typescript: typeof ts,
  ) => NonNullable<OpenAPITSOptions['transformProperty']>
  reached: (schemas: Record<string, unknown>, node: unknown, seen?: Set<string>) => Set<string>
  unescapePointer: (segment: string) => string
}

// A computed specifier, so tsc does not look for a declaration of the plain .mjs; the
// cast gives the test the module's shape, so a call that does not fit it fails tsc.
const shared = (await import(
  new URL('../../scripts/gen-api-request-defaults.mjs', import.meta.url).href
)) as Shared

const ref = (name: string) => ({ $ref: `#/components/schemas/${name}` })
const body = (name: string) => ({ content: { 'application/json': { schema: ref(name) } } })

const doc = {
  openapi: '3.1.0',
  info: { title: 'test', version: '1' },
  paths: {
    '/a': { post: { requestBody: body('Req'), responses: { '200': { description: 'ok', ...body('Shared') } } } },
    '/b': { put: { requestBody: body('Shared'), responses: { '204': { description: 'none' } } } },
  },
  components: {
    schemas: {
      Req: {
        type: 'object',
        required: ['name', 'a/b'],
        properties: {
          count: { type: 'integer', default: 1 },
          name: { type: 'string', default: 'x' },
          'a/b': { type: 'integer', default: 2 },
          'c/d': { type: 'integer', default: 3 },
          child: ref('Nested'),
        },
      },
      Nested: { type: 'object', properties: { size: { type: 'integer', default: 4 } } },
      Shared: { type: 'object', properties: { flag: { type: 'boolean', default: false } } },
    },
  },
}

async function generated(): Promise<string> {
  return astToString(await openapiTS(doc as never, { transformProperty: shared.requestDefaultsHook(doc, ts) }))
}

/** The members of one component schema's type, one per line, comments dropped. */
function members(out: string, name: string): string[] {
  const start = out.indexOf(`        ${name}: {`)
  const end = out.indexOf('\n        };', start)
  return out
    .slice(start, end)
    .split('\n')
    .slice(1)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('/**') && !line.startsWith('*'))
}

describe('gen-api request defaults', () => {
  it('finds the schemas a $ref reaches, through nested refs', () => {
    expect([...shared.reached(doc.components.schemas, ref('Req'))].sort()).toEqual(['Nested', 'Req'])
  })

  it('makes a defaulted property optional only where request bodies alone reach its schema', async () => {
    const out = await generated()
    expect(members(out, 'Req')).toEqual([
      'count?: number;',
      'name: string;',
      '"a/b": number;',
      '"c/d"?: number;',
      'child?: components["schemas"]["Nested"];',
    ])
    expect(members(out, 'Nested')).toEqual(['size?: number;'])
    expect(members(out, 'Shared')).toEqual(['flag: boolean;'])
  })

  it('unescapes a JSON-pointer segment, ~1 before ~0', () => {
    expect(shared.unescapePointer('a~1b')).toBe('a/b')
    expect(shared.unescapePointer('a~01')).toBe('a~1')
  })
})
