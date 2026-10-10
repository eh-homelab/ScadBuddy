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
})
