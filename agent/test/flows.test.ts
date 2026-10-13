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

  it("reads a flow's history and restores a commit as a write (#2503)", async () => {
    expect(
      ['list_flow_versions', 'get_flow_version_source', 'diff_flow_version', 'restore_flow_version'].map(
        (n) => tool(n).risk,
      ),
    ).toEqual(['read', 'read', 'read', 'write'])
    const COMMIT = 'a'.repeat(40)
    const restored: string[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/workflows/names/swap/versions`, ({ request }) =>
        HttpResponse.json(
          [2, 1].map((version) => ({
            id: DEFINITION,
            commit: String(version).repeat(40),
            short: String(version).repeat(7),
            version,
            message: 'Register swap',
            created_by: { kind: 'browser' },
            created_at: '2026-10-11T00:00:00Z',
            current: version === 2 && new URL(request.url).searchParams.get('limit') === '500',
          })),
        ),
      ),
      http.get(`${BACKEND}/api/v1/workflows/names/swap/versions/${COMMIT}/diff`, ({ request }) =>
        HttpResponse.json({ name: 'swap', base: new URL(request.url).searchParams.get('base'), head: COMMIT, patch: '' }),
      ),
      http.post(`${BACKEND}/api/v1/workflows/names/swap/versions/${COMMIT}/restore`, ({ request }) => {
        restored.push(request.url)
        return HttpResponse.json({ id: DEFINITION, name: 'swap', version: 3, commit: 'c'.repeat(40), script: 'x' })
      }),
    )
    const listed = firstText(await runTool(tool('list_flow_versions'), { name: 'swap' }, ctx())) as {
      items: { version: number; current: boolean }[]
    }
    expect(listed.items.map((v) => [v.version, v.current])).toEqual([
      [2, true],
      [1, false],
    ])
    const base = 'b'.repeat(40)
    const diff = await runTool(tool('diff_flow_version'), { name: 'swap', commit: COMMIT, base }, ctx())
    expect((firstText(diff) as { base: string }).base).toBe(base)
    const done = await runTool(tool('restore_flow_version'), { name: 'swap', commit: COMMIT }, ctx())
    expect((firstText(done) as { version: number }).version).toBe(3)
    expect(restored).toHaveLength(1)
  })

  it('archives as a write and deletes as an outward call (#2485)', async () => {
    expect([tool('archive_flow').risk, tool('unarchive_flow').risk, tool('delete_flow').risk]).toEqual([
      'write',
      'write',
      'outward',
    ])
    expect(tool('delete_flow').summarize?.({ name: 'swap' })).toContain('swap')
    const calls: string[] = []
    const state = (name: string, extra: Record<string, unknown>) => ({ name, archived_at: null, deleted_at: null, ...extra })
    server.use(
      http.get(`${BACKEND}/api/v1/workflows`, ({ request }) => {
        calls.push(`list ${new URL(request.url).searchParams.get('archived')}`)
        return HttpResponse.json([])
      }),
      http.post(`${BACKEND}/api/v1/workflows/names/swap/archive`, () => {
        calls.push('archive')
        return HttpResponse.json(state('swap', { archived_at: '2026-10-12T00:00:00Z' }))
      }),
      http.post(`${BACKEND}/api/v1/workflows/names/swap/unarchive`, () => {
        calls.push('unarchive')
        return HttpResponse.json(state('swap', {}))
      }),
      http.delete(`${BACKEND}/api/v1/workflows/names/swap`, () => {
        calls.push('delete')
        return HttpResponse.json(state('swap', { deleted_at: '2026-10-12T00:00:00Z', deleted_commit: 'd'.repeat(40) }))
      }),
    )
    await runTool(tool('list_flows'), {}, ctx())
    await runTool(tool('list_flows'), { include_archived: true }, ctx())
    const archived = firstText(await runTool(tool('archive_flow'), { name: 'swap' }, ctx())) as { archived_at: string }
    expect(archived.archived_at).toBe('2026-10-12T00:00:00Z')
    await runTool(tool('unarchive_flow'), { name: 'swap' }, ctx())
    // Gated: nothing is sent until a person approves, then confirm_action runs it once.
    expect(tool('delete_flow').gated).toBe(true)
    expect((await runTool(tool('delete_flow'), { name: 'swap' }, ctx())).isError).toBe(true)
    expect(calls).not.toContain('delete')
    const deleted = firstText(await tool('delete_flow').execute({ name: 'swap' }, ctx())) as { deleted_commit: string }
    expect(deleted.deleted_commit).toBe('d'.repeat(40))
    expect(calls).toEqual(['list null', 'list true', 'archive', 'unarchive', 'delete'])
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

  it('lists the reset points as a read (#2350)', async () => {
    expect(tool('get_flow_reset_points').risk).toBe('read')
    const RUN = '7a1c2b0e-1d2a-4c3b-9e8f-0a1b2c3d4e5f'
    const answer = { workflow_run_id: 'w1', as_of_event_id: 40, points: [{ event_id: 12, first_step: 1 }] }
    server.use(http.get(`${BACKEND}/api/v1/workflow-runs/${RUN}/reset-points`, () => HttpResponse.json(answer)))
    const result = await runTool(tool('get_flow_reset_points'), { run_id: RUN }, ctx())
    expect(firstText(result)).toEqual(answer)
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

describe('run grants (#2481)', () => {
  const RUN = '1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed'
  const GRANT = {
    expires_at: '2026-10-13T12:00:00Z',
    sessions: true,
    rules: [{ fn: 'queue_print', printers: [3], max_prints: 4 }],
  }

  it('proposes a grant with a start: only a person approves it', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/workflows/${DEFINITION}/runs`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: RUN, status: 'starting', repeated: false }, { status: 202 })
      }),
    )
    const result = await runTool(tool('start_flow_run'), { definition_id: DEFINITION, grant: GRANT }, ctx())
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ grant: { ...GRANT, ask_starter_session: true } }])
    expect(tool('start_flow_run').description).toMatch(/requested/)
  })

  it('refuses an expiry with no offset and an empty value list before the round trip', async () => {
    let sent = 0
    server.use(
      http.post(`${BACKEND}/api/v1/workflows/${DEFINITION}/runs`, () => {
        sent += 1
        return HttpResponse.json({ id: RUN, status: 'starting', repeated: false }, { status: 202 })
      }),
    )
    const naive = { ...GRANT, expires_at: '2026-10-13T12:00:00' }
    expect((await runTool(tool('start_flow_run'), { definition_id: DEFINITION, grant: naive }, ctx())).isError).toBe(true)
    const empty = { ...GRANT, rules: [{ fn: 'tool', tool: 'send_to_bambuddy', args: [{ path: '/x', in: [] }] }] }
    expect((await runTool(tool('start_flow_run'), { definition_id: DEFINITION, grant: empty }, ctx())).isError).toBe(true)
    expect(sent).toBe(0)
  })

  it('revokes as a write; approving and declining have no tool', async () => {
    expect(tool('revoke_flow_grant').risk).toBe('write')
    const routes = ALL_TOOLS.flatMap((t) => t.routes)
    expect(routes).toContain('POST /api/v1/workflow-runs/{run_id}/grant/revoke')
    expect(routes).not.toContain('POST /api/v1/workflow-runs/{run_id}/grant/approve')
    expect(routes).not.toContain('POST /api/v1/workflow-runs/{run_id}/grant/decline')
    const revoked: string[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/workflow-runs/${RUN}/grant/revoke`, ({ request }) => {
        revoked.push(request.url)
        return HttpResponse.json({ id: 'g1', status: 'approved', revoked_by: 'agent test', in_force: false })
      }),
    )
    const result = await runTool(tool('revoke_flow_grant'), { run_id: RUN }, ctx())
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toMatchObject({ id: 'g1', in_force: false })
    expect(revoked).toHaveLength(1)
  })
})

describe('typed flow inputs (#2483)', () => {
  const SCHEMA = {
    type: 'object',
    properties: { slug: { type: 'string' }, copies: { type: 'integer', default: 1 } },
    required: ['slug'],
  }

  it('registers and checks a script with its inputs schema', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/workflows`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: DEFINITION, name: 'swap', version: 1, inputs_schema: SCHEMA }, { status: 201 })
      }),
      http.post(`${BACKEND}/api/v1/workflow-script/check`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ problems: [] })
      }),
    )
    await runTool(tool('check_flow_script'), { script: 's', inputs_schema: SCHEMA }, ctx())
    const result = await runTool(tool('register_flow'), { name: 'swap', script: 's', inputs_schema: SCHEMA }, ctx())
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([
      { script: 's', inputs_schema: SCHEMA },
      { name: 'swap', script: 's', inputs_schema: SCHEMA },
    ])
    expect(tool('register_flow').description).toMatch(/inputs\(\)/)
  })

  it("starts a run with its inputs and reports the ones that do not fit", async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/workflows/${DEFINITION}/runs`, async ({ request }) => {
        const body = (await request.json()) as { inputs?: { slug?: unknown } }
        bodies.push(body)
        if (typeof body.inputs?.slug !== 'string') {
          return HttpResponse.json(
            { status: 422, detail: "The run's inputs do not fit the flow's inputs schema: inputs: 'slug' is a required property", problems: ["inputs: 'slug' is a required property"] },
            { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }
        return HttpResponse.json({ id: 'r1', status: 'starting', repeated: false }, { status: 202 })
      }),
    )
    const started = await runTool(tool('start_flow_run'), { definition_id: DEFINITION, inputs: { slug: 'box' } }, ctx())
    expect(started.isError).toBeFalsy()
    const refused = await runTool(tool('start_flow_run'), { definition_id: DEFINITION, inputs: {} }, ctx())
    expect(refused.isError).toBe(true)
    expect(JSON.stringify(refused.content)).toContain('required property')
    expect(bodies).toEqual([{ inputs: { slug: 'box' } }, { inputs: {} }])
  })
})
