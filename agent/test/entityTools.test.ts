import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, ToolError, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// Entity attachments and links (#6950, phase 1 Task 7): the generic `/entities/{ref}` routes.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const SPOOL = 'spool:11111111-2222-4333-8444-555555555555'
const PRINTER = 'printer:7'
const ID = 'a77ac4ed-1111-4222-8333-944455556666'
const base = (entity: string) => `${BACKEND}/api/v1/entities/${encodeURIComponent(entity)}`

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
    pollIntervalMs: 1,
  }
}

describe('entity attachment and link tools', () => {
  it('tiers lists as read and every change as outward, and maps each to its route', () => {
    const expected: Record<string, [string, string]> = {
      entity_attachment_list: ['read', 'GET /api/v1/entities/{ref}/attachments'],
      entity_attachment_update: ['outward', 'PATCH /api/v1/entities/{ref}/attachments/{attachment_id}'],
      entity_attachment_delete: ['outward', 'DELETE /api/v1/entities/{ref}/attachments/{attachment_id}'],
      entity_link_list: ['read', 'GET /api/v1/entities/{ref}/links'],
      entity_link_add: ['outward', 'POST /api/v1/entities/{ref}/links'],
      entity_link_update: ['outward', 'PATCH /api/v1/entities/{ref}/links/{link_id}'],
      entity_link_delete: ['outward', 'DELETE /api/v1/entities/{ref}/links/{link_id}'],
    }
    for (const [name, [risk, route]] of Object.entries(expected)) {
      expect(tool(name).risk, name).toBe(risk)
      expect(tool(name).routes, name).toEqual([route])
    }
  })

  it('lists attachments and links with the entity page where the kind has one', async () => {
    server.use(
      http.get(`${base(SPOOL)}/attachments`, () => HttpResponse.json({ attachments: [{ id: ID, caption: 'x' }] })),
      http.get(`${base(PRINTER)}/links`, () => HttpResponse.json({ links: [{ id: ID, url: 'https://e.test/' }] })),
    )
    const attachments = firstText(await tool('entity_attachment_list').execute({ entity: SPOOL }, ctx()))
    expect(attachments).toEqual({
      attachments: [{ id: ID, caption: 'x' }],
      entity_ui_path: '/filament/spools/11111111-2222-4333-8444-555555555555',
    })
    const links = firstText(await tool('entity_link_list').execute({ entity: PRINTER }, ctx()))
    expect(links).toEqual({ links: [{ id: ID, url: 'https://e.test/' }] })
  })

  it('sends each change to its route with only the fields given', async () => {
    const calls: string[] = []
    server.use(
      http.patch(`${base(SPOOL)}/attachments/${ID}`, async ({ request }) => {
        calls.push(`PATCH att ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ID })
      }),
      http.delete(`${base(SPOOL)}/attachments/${ID}`, () => {
        calls.push('DELETE att')
        return new HttpResponse(null, { status: 204 })
      }),
      http.post(`${base(SPOOL)}/links`, async ({ request }) => {
        calls.push(`POST link ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ID })
      }),
      http.patch(`${base(SPOOL)}/links/${ID}`, async ({ request }) => {
        calls.push(`PATCH link ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ID })
      }),
      http.delete(`${base(SPOOL)}/links/${ID}`, () => {
        calls.push('DELETE link')
        return new HttpResponse(null, { status: 204 })
      }),
    )
    await tool('entity_attachment_update').execute({ entity: SPOOL, attachment_id: ID, caption: 'Label' }, ctx())
    expect(firstText(await tool('entity_attachment_delete').execute({ entity: SPOOL, attachment_id: ID }, ctx()))).toEqual({ deleted: ID })
    await tool('entity_link_add').execute({ entity: SPOOL, url: 'https://e.test/p', kind: 'support' }, ctx())
    await tool('entity_link_update').execute({ entity: SPOOL, link_id: ID, note: 'n' }, ctx())
    expect(firstText(await tool('entity_link_delete').execute({ entity: SPOOL, link_id: ID }, ctx()))).toEqual({ deleted: ID })
    expect(calls).toEqual([
      'PATCH att {"caption":"Label"}',
      'DELETE att',
      'POST link {"url":"https://e.test/p","kind":"support"}',
      'PATCH link {"note":"n"}',
      'DELETE link',
    ])
    await expect(tool('entity_link_update').execute({ entity: SPOOL, link_id: ID }, ctx())).rejects.toThrow('nothing to change')
  })

  it('names the entity when the backend says entity_not_found', async () => {
    server.use(
      http.get(`${base(SPOOL)}/links`, () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/entities/entity_not_found', title: 'Not found', status: 404, detail: 'gone' },
          { status: 404 },
        ),
      ),
    )
    const err = await tool('entity_link_list').execute({ entity: SPOOL }, ctx()).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ToolError)
    expect((err as ToolError).message).toContain(`no entity ${SPOOL}`)
    expect((err as ToolError).status).toBe(404)
    expect((err as ToolError).problem).toBe('entity_not_found')
  })

  it('parks entity_link_add as a pending action and sends nothing', async () => {
    const result = await runTool(tool('entity_link_add'), { entity: SPOOL, url: 'https://e.test/p' }, ctx())
    expect(firstText(result)).toMatchObject({ status: 'pending_approval' })
  })
})

// Entity properties (#7026, Task 4): an entity's key-value properties and a kind's schema.
describe('entity property tools', () => {
  const TYPE = 'spool_type:a1b2c3d4-0000-4000-8000-000000000001'
  const KIND = `${BACKEND}/api/v1/entity-kinds/spool_type/property-schema`

  it('tiers the reads as read and the two writes as write, and maps each to its route', () => {
    const expected: Record<string, [string, string]> = {
      entity_property_list: ['read', 'GET /api/v1/entities/{ref}/properties'],
      entity_property_set: ['write', 'PATCH /api/v1/entities/{ref}/properties'],
      entity_property_schema_get: ['read', 'GET /api/v1/entity-kinds/{kind}/property-schema'],
      entity_property_schema_set: ['write', 'PUT /api/v1/entity-kinds/{kind}/property-schema'],
    }
    for (const [name, [risk, route]] of Object.entries(expected)) {
      expect(tool(name).risk, name).toBe(risk)
      expect(tool(name).routes, name).toEqual([route])
    }
  })

  it('lists the properties with the entity page, and renames a key in one call', async () => {
    const bodies: unknown[] = []
    const row = { key: 'gtin', value: ['0123'], source: 'manual' }
    server.use(
      http.get(`${base(TYPE)}/properties`, () => HttpResponse.json({ properties: [row] })),
      http.patch(`${base(TYPE)}/properties`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ properties: [{ ...row, key: 'ean' }] })
      }),
    )
    expect(firstText(await tool('entity_property_list').execute({ entity: TYPE }, ctx()))).toEqual({
      properties: [row],
      entity_ui_path: '/filament/types/a1b2c3d4-0000-4000-8000-000000000001',
    })
    const renamed = firstText(await runTool(tool('entity_property_set'), { entity: TYPE, set: { gtin: null, ean: ['0123'] } }, ctx()))
    expect(renamed).toMatchObject({ properties: [{ key: 'ean' }] })
    expect(bodies).toEqual([{ set: { gtin: null, ean: ['0123'] } }])
  })

  it('refuses a bad key or an empty set before it sends anything', () => {
    expect(() => tool('entity_property_set').parse({ entity: TYPE, set: { 'Bad-Key': 'x' } })).toThrow()
    expect(() => tool('entity_property_set').parse({ entity: TYPE, set: {} })).toThrow()
    expect(() => tool('entity_property_set').parse({ entity: TYPE, set: { td: 1.8, ok: true, lot: 'L1', gtins: ['a'], gone: null } })).not.toThrow()
  })

  it("reads and replaces a kind's schema", async () => {
    const bodies: unknown[] = []
    const schema = { type: 'object', properties: { td: { type: 'number' } } }
    server.use(
      http.get(KIND, () => HttpResponse.json({ kind: 'spool_type', schema, keys_in_use: [{ key: 'td', count: 2 }] })),
      http.put(KIND, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ kind: 'spool_type', schema, keys_in_use: [] })
      }),
    )
    expect(firstText(await tool('entity_property_schema_get').execute({ kind: 'spool_type' }, ctx()))).toMatchObject({
      keys_in_use: [{ key: 'td', count: 2 }],
    })
    await runTool(tool('entity_property_schema_set'), { kind: 'spool_type', schema }, ctx())
    expect(bodies).toEqual([{ schema }])
  })
})
