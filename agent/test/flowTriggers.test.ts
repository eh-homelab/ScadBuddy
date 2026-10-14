import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { NOT_A_TOOL } from '../src/tools/coverage.js'
import type { Tool, ToolContext } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'

// Flow triggers (#2484, spec 2026-10-11 §12.5): making, editing or resuming a trigger and
// attaching its standing grant set up unattended runs, so they are outward; approving or
// declining a standing grant has no tool at all.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const DEFINITION = '6f1c2b0e-1d2a-4c3b-9e8f-0a1b2c3d4e5f'
const TRIGGER = '0d8f6e1a-2b3c-4d5e-8f90-a1b2c3d4e5f6'

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

describe('flow trigger tools (#2484)', () => {
  it('tiers each as the spec does', () => {
    const tiers = Object.fromEntries(
      [
        'list_flow_triggers',
        'get_flow_trigger',
        'create_flow_trigger',
        'update_flow_trigger',
        'pause_flow_trigger',
        'resume_flow_trigger',
        'delete_flow_trigger',
        'attach_flow_trigger_grant',
        'revoke_flow_trigger_grant',
      ].map((n) => [n, tool(n).risk]),
    )
    expect(tiers).toEqual({
      list_flow_triggers: 'read',
      get_flow_trigger: 'read',
      create_flow_trigger: 'outward',
      update_flow_trigger: 'outward',
      pause_flow_trigger: 'write',
      resume_flow_trigger: 'outward',
      delete_flow_trigger: 'write',
      attach_flow_trigger_grant: 'outward',
      revoke_flow_trigger_grant: 'write',
    })
  })

  it('gives no tool to approve or decline a standing grant', () => {
    const routes = ALL_TOOLS.flatMap((t) => t.routes)
    for (const action of ['approve', 'decline']) {
      const operation = `POST /api/v1/flow-triggers/{trigger_id}/grant/${action}`
      expect(routes).not.toContain(operation)
      expect(NOT_A_TOOL.map((n) => n.operation)).toContain(operation)
    }
  })

  it('sends a trigger with no author or grant status of its own', async () => {
    let sent: unknown
    server.use(
      http.post(`${BACKEND}/api/v1/flow-triggers`, async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ id: TRIGGER }, { status: 201 })
      }),
    )
    // Run as an approved call runs it: outward, it would otherwise wait for the user.
    await tool('create_flow_trigger').execute(
      { definition_id: DEFINITION, schedule: { cron: ['0 9 * * 1'], time_zone: 'Europe/London' } },
      ctx(),
    )
    expect(sent).toEqual({
      definition_id: DEFINITION,
      schedule: { cron: ['0 9 * * 1'], time_zone: 'Europe/London', offset_s: 0, catchup_window_s: 300 },
      overlap: 'skip',
    })
  })

  it('refuses fields the routes do not take', () => {
    // An author the model makes up is dropped: the backend names the author from the request.
    const parsed = tool('create_flow_trigger').parse({
      definition_id: DEFINITION,
      schedule: { interval_s: 3600 },
      created_by: 'browser',
    })
    expect('created_by' in parsed).toBe(false)
    expect(() =>
      tool('attach_flow_trigger_grant').parse({
        trigger_id: TRIGGER,
        grant: { expires_at: '2026-11-01T00:00:00Z', rules: [], status: 'approved' },
      }),
    ).toThrow()
  })
})
