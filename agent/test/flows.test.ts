import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// Flows (#1057): register, list and start flows, read their runs (backend/scadbuddy/api/flows.py).

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const DEFINITION = '6f1c2b0e-1d2a-4c3b-9e8f-0a1b2c3d4e5f'

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(overrides: Partial<ToolContext> = {}): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
    pollIntervalMs: 1,
    ...overrides,
  }
}

describe('flow tools (#1057)', () => {
  it('reads with the read tools and writes with register and start, never delete', () => {
    expect(['list_flows', 'get_flow', 'list_flow_runs', 'get_flow_run'].map((n) => tool(n).risk)).toEqual([
      'read',
      'read',
      'read',
      'read',
    ])
    expect([tool('register_flow').risk, tool('start_flow_run').risk]).toEqual(['write', 'write'])
    expect(ALL_TOOLS.flatMap((t) => t.routes)).not.toContain('DELETE /api/v1/workflow-runs/{run_id}')
  })

  it('starts a run with one key, re-sent after a still-accepting answer', async () => {
    const seen: { key: string | null; body: string }[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/workflows/${DEFINITION}/runs`, async ({ request }) => {
        seen.push({ key: request.headers.get('Idempotency-Key'), body: await request.text() })
        if (seen.length === 1) {
          return HttpResponse.json(
            { type: 'https://scadbuddy.dev/problems/command-still-accepting', status: 503 },
            { status: 503, headers: { 'Retry-After': '0', 'Content-Type': 'application/problem+json' } },
          )
        }
        return HttpResponse.json({ id: 'r1', status: 'starting', repeated: false }, { status: 202 })
      }),
    )
    const result = await runTool(tool('start_flow_run'), { definition_id: DEFINITION, approval_timeout: 'never' }, ctx())
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toEqual({ id: 'r1', status: 'starting', repeated: false })
    expect(seen).toHaveLength(2)
    expect(seen[0]!.key).toMatch(/^[0-9a-f]{32}$/)
    expect(seen[1]!.key).toBe(seen[0]!.key)
    expect(JSON.parse(seen[0]!.body)).toEqual({ approval_timeout: 'never' })
  })

  it('registers a script and reports its problems', async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/workflows`, () =>
        HttpResponse.json(
          { status: 422, detail: 'The script does not type-check.', problems: [{ line: 3, message: 'x' }] },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const result = await runTool(tool('register_flow'), { name: 'swap', script: 'x' }, ctx())
    expect(result.isError).toBe(true)
  })

  it('previews as a read and resets as an outward call with one key', async () => {
    expect([tool('preview_flow_reset').risk, tool('reset_flow_run').risk]).toEqual(['read', 'outward'])
    const RUN = '7a1c2b0e-1d2a-4c3b-9e8f-0a1b2c3d4e5f'
    const seen: { key: string | null; body: unknown }[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/workflow-runs/${RUN}/reset-preview`, ({ request }) =>
        HttpResponse.json({
          event_id: Number(new URL(request.url).searchParams.get('event_id')),
          as_of_event_id: 40,
          workflow_run_id: 'w1',
          valid: true,
          calls: [{ fn: 'queue_print', call_id: 'c1', scheduled_event_id: 30 }],
        }),
      ),
      http.post(`${BACKEND}/api/v1/workflow-runs/${RUN}/reset`, async ({ request }) => {
        seen.push({ key: request.headers.get('Idempotency-Key'), body: await request.json() })
        return HttpResponse.json({ run_id: RUN, workflow_run_id: 'w2', event_id: 12 })
      }),
    )
    const preview = await runTool(tool('preview_flow_reset'), { run_id: RUN, event_id: 12 }, ctx())
    expect(firstText(preview)).toMatchObject({ event_id: 12, as_of_event_id: 40, workflow_run_id: 'w1' })
    const args = { run_id: RUN, event_id: 12, as_of_event_id: 40, workflow_run_id: 'w1' }
    // Gated like every outward tool: nothing is sent until a person approves.
    expect(tool('reset_flow_run').gated).toBe(true)
    const below = await runTool(tool('reset_flow_run'), args, ctx())
    expect(below.isError).toBe(true)
    expect(seen).toEqual([])
    // As confirm_action runs it once a person approved.
    const reset = await tool('reset_flow_run').execute(args, ctx())
    expect(firstText(reset)).toEqual({ run_id: RUN, workflow_run_id: 'w2', event_id: 12 })
    expect(seen).toEqual([{ key: expect.stringMatching(/^[0-9a-f]{32}$/), body: { event_id: 12, as_of_event_id: 40, workflow_run_id: 'w1' } }])
  })
})
