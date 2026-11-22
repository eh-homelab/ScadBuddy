import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import type { Tool, ToolContext } from '../src/tools/registry.js'
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
    await expect(tool('entity_link_list').execute({ entity: SPOOL }, ctx())).rejects.toThrow(`no entity ${SPOOL}`)
  })
})
