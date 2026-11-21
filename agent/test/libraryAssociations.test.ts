import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { pinnedOutward } from '../src/tools/pinnedOutward.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { ASSOCIATION_RELATIONS } from '../src/tools/print.js'
import type { Tool, ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #6395 — a native library file associated with a model or another file: add, list
// (a file's or a model's), remove. ScadBuddy's own re-settable records, so `write`.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
  }
}

const FILE = 1_000_001_498
const ID = '0b6f1c62-6a43-4bd4-9a83-5f0c4cbb2f10'
const ROW = {
  id: ID,
  library_file_id: FILE,
  library_file_name: 'fixed.3mf',
  relation: 'output_of',
  target: 'model:unicorn-pencil-holder',
  target_name: 'Unicorn pencil holder',
  note: 'scaled',
  created_at: '2026-11-15T10:00:00Z',
  created_by: { kind: 'agent', principal: 'test', session: null, run_id: null },
  implied_by: null,
}

describe('library file associations (#6395)', () => {
  it("takes the relations the API's spec lists", () => {
    const spec = JSON.parse(readFileSync(new URL('../../backend/openapi.json', import.meta.url), 'utf8')) as {
      components: { schemas: { AssociationRequest: { properties: { relation: { enum: string[] } } } } }
    }
    expect([...ASSOCIATION_RELATIONS]).toEqual(spec.components.schemas.AssociationRequest.properties.relation.enum)
  })

  it('associates a file with a model, its relation said from the file', async () => {
    let sent: unknown
    server.use(
      http.post(`${BACKEND}/api/v1/print/library/${FILE}/associations`, async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json(ROW, { status: 201 })
      }),
    )
    const t = tool('associate_library_file')
    expect(t.risk).toBe('write')
    const input = { file_id: FILE, target: 'model:unicorn-pencil-holder', relation: 'output_of', note: 'scaled' }
    expect(firstText(await t.execute(input, ctx()))).toEqual(ROW)
    expect(sent).toEqual({ target: 'model:unicorn-pencil-holder', relation: 'output_of', note: 'scaled' })
    expect(t.summarize(input)).toBe(`Associate library file ${FILE}: output_of model:unicorn-pencil-holder`)
    expect(pinnedOutward('associate_library_file', input)).toBe(false)
  })

  it('refuses a target or relation outside the lists before anything runs', () => {
    const t = tool('associate_library_file')
    expect(() => t.parse({ file_id: FILE, target: 'output:abc', relation: 'output_of' })).toThrow()
    expect(() => t.parse({ file_id: FILE, target: 'library:5', relation: 'copy_of' })).toThrow()
    expect(() => t.parse({ file_id: FILE, target: 'library:01000000001', relation: 'variant_of' })).toThrow()
    expect(t.parse({ file_id: FILE, target: 'library:1000000001', relation: 'variant_of' })).toMatchObject({
      target: 'library:1000000001',
    })
  })

  it("lists a file's or a model's associations, naming one of them", async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/print/library/${FILE}/associations`, () => HttpResponse.json({ associations: [ROW] })),
      http.get(`${BACKEND}/api/v1/models/unicorn-pencil-holder/associations`, () =>
        HttpResponse.json({ associations: [] }),
      ),
    )
    const t = tool('list_associations')
    expect(t.risk).toBe('read')
    expect(firstText(await t.execute({ file_id: FILE }, ctx()))).toMatchObject({ associations: [ROW], next_cursor: null })
    expect(firstText(await t.execute({ slug: 'unicorn-pencil-holder' }, ctx()))).toMatchObject({ associations: [] })
    expect(() => t.parse({})).toThrow(/name one/)
    expect(() => t.parse({ file_id: FILE, slug: 'x' })).toThrow(/name one/)
  })

  it('removes one by id', async () => {
    let removed = false
    server.use(
      http.delete(`${BACKEND}/api/v1/print/library/associations/${ID}`, () => {
        removed = true
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const t = tool('remove_association')
    expect(t.risk).toBe('write')
    expect(firstText(await t.execute({ association_id: ID }, ctx()))).toEqual({ removed: ID })
    expect(removed).toBe(true)
    expect(() => t.parse({ association_id: 'not-a-uuid' })).toThrow()
  })
})
