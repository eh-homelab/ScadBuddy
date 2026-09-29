import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { AUTHOR_HEADER, AUTHOR_SESSION_HEADER, principalHeader } from '../src/tools/authorship.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { SERVER_NAME } from '../src/tools/projections.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #252's edit loop: apply_patch (with its conflict answer), checkpoint, the
// `base` on update_source, and agent authorship of every backend call.

type Seen = { method: string; path: string; headers: Headers; body: unknown }

const RECORD = { slug: 'box', name: 'Box', origin: 'user', version: 'a'.repeat(40), has_thumbnail: false, has_readme: false, updated_at: 'now' }

function backend(answer: (seen: Seen) => Response = () => Response.json(RECORD)) {
  const seen: Seen[] = []
  const client = createBackendClient(BACKEND, async (request) => {
    const r = request as Request
    const text = await r.text()
    const entry = { method: r.method, path: new URL(r.url).pathname, headers: r.headers, body: text ? JSON.parse(text) : undefined }
    seen.push(entry)
    return answer(entry)
  })
  return { client, seen }
}

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(client: ReturnType<typeof backend>['client'], overrides: Partial<ToolContext> = {}): ToolContext {
  return {
    ...services({ backend: client }),
    principal: { id: 'token:t1', kind: 'bearer', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...overrides,
  }
}

function content(result: Awaited<ReturnType<typeof runTool>>): unknown {
  return firstText(result)
}

describe('apply_patch', () => {
  it('sends a diff against its base and answers the new record', async () => {
    const { client, seen } = backend()
    const diff = '@@ -1 +1 @@\n-cube(1);\n+cube(2);\n'
    const result = await runTool(tool('apply_patch'), { slug: 'box', base: 'abc1234', patch: diff, message: 'Bigger' }, ctx(client))
    expect(result.isError, JSON.stringify(result)).toBeFalsy()
    expect(seen.map((s) => `${s.method} ${s.path}`)).toEqual(['POST /api/v1/models/box/source/patch'])
    expect(seen[0]!.body).toEqual({ base: 'abc1234', patch: diff, edits: null, message: 'Bigger', force: false })
    expect(content(result)).toMatchObject({ slug: 'box', version: RECORD.version })
  })

  it('answers a moved base as a conflict naming the current revision', async () => {
    const current = 'b'.repeat(40)
    const { client } = backend(() =>
      Response.json({ status: 409, title: 'Conflict', detail: 'moved on', base: 'abc1234', current }, { status: 409 }),
    )
    const result = await runTool(
      tool('apply_patch'),
      { slug: 'box', base: 'abc1234', edits: [{ search: 'cube(1)', replace: 'cube(2)' }] },
      ctx(client),
    )
    expect(result.isError).toBe(true)
    expect(content(result)).toMatchObject({ status: 'conflict', base: 'abc1234', current })
  })

  it('passes any other refusal through with the backend reason', async () => {
    const { client } = backend(() =>
      Response.json({ status: 422, title: 'Unprocessable Content', detail: 'the patch does not apply: hunk 1' }, { status: 422 }),
    )
    const result = await runTool(tool('apply_patch'), { slug: 'box', base: 'abc1234', patch: '@@ -1 +1 @@\n-x\n+y\n' }, ctx(client))
    expect(result.isError).toBe(true)
    expect(String(firstText(result))).toContain('HTTP 422')
    expect(String(firstText(result))).toContain('hunk 1')
  })

  it('needs exactly one of patch and edits, before calling the backend', async () => {
    const { client, seen } = backend()
    const neither = await runTool(tool('apply_patch'), { slug: 'box', base: 'abc1234' }, ctx(client))
    const both = await runTool(
      tool('apply_patch'),
      { slug: 'box', base: 'abc1234', patch: '@@', edits: [{ search: 'a', replace: 'b' }] },
      ctx(client),
    )
    expect([neither.isError, both.isError]).toEqual([true, true])
    expect(String(firstText(neither))).toContain('exactly one')
    expect(seen).toEqual([])
  })
})

describe('checkpoint', () => {
  it("answers the model's current revision and how to go back to it", async () => {
    const { client } = backend()
    const result = await runTool(tool('checkpoint'), { slug: 'box', label: 'try a chamfer' }, ctx(client))
    expect(content(result)).toEqual({
      slug: 'box',
      checkpoint: RECORD.version,
      label: 'try a chamfer',
      restore: { tool: 'restore_version', arguments: { slug: 'box', commit: RECORD.version } },
    })
    expect(tool('checkpoint').risk).toBe('read')
  })

  it('refuses a model with no history', async () => {
    const { client } = backend(() => Response.json({ ...RECORD, version: null }))
    const result = await runTool(tool('checkpoint'), { slug: 'box' }, ctx(client))
    expect(result.isError).toBe(true)
    expect(String(firstText(result))).toContain('no revision history')
  })
})

describe('update_source', () => {
  it('sends the base it was given', async () => {
    const { client, seen } = backend()
    await runTool(tool('update_source'), { slug: 'box', source: 'cube(3);', base: 'abc1234' }, ctx(client))
    expect(seen[0]!.body).toEqual({ source: 'cube(3);', message: null, force: false, base: 'abc1234' })
  })
})

describe('agent authorship (#252)', () => {
  it('names the principal and session on every backend call a tool makes', async () => {
    const { client, seen } = backend()
    await runTool(tool('update_source'), { slug: 'box', source: 'cube(3);' }, ctx(client, { session: 'sess-1' }))
    expect(seen[0]!.headers.get(AUTHOR_HEADER)).toBe('token:t1')
    expect(seen[0]!.headers.get(AUTHOR_SESSION_HEADER)).toBe('sess-1')
  })

  it('leaves out the session outside one, and never sends the headless-browser marker', async () => {
    const { client, seen } = backend()
    await runTool(tool('get_model'), { slug: 'box' }, ctx(client))
    expect(seen[0]!.headers.get(AUTHOR_HEADER)).toBe('token:t1')
    expect(seen[0]!.headers.has(AUTHOR_SESSION_HEADER)).toBe(false)
    expect(seen[0]!.headers.has('X-ScadBuddy-Agent-Session')).toBe(false)
  })

  it('percent-encodes what the backend would refuse in a principal id', () => {
    expect(principalHeader('oidc:https://idp.example#ünï code')).toBe('oidc:https://idp.example#%C3%BCn%C3%AF%20code')
    expect(principalHeader('x'.repeat(400))).toHaveLength(300)
  })

  it("carries the harness session's id through the in-process server", async () => {
    const { client, seen } = backend()
    const wired = harnessTools(services({ backend: client }))
    const servers = wired.mcpServers({ id: 'sess-42', owner: { kind: 'browser', id: 'browser', label: 'You' } })
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await servers[SERVER_NAME]!.instance.connect(serverSide)
    const mcp = new Client({ name: 'authoring-test', version: '0' })
    await mcp.connect(clientSide)
    await mcp.callTool({ name: 'get_model', arguments: { slug: 'box' } })
    await mcp.close()
    expect(seen[0]!.headers.get(AUTHOR_HEADER)).toBe('browser')
    expect(seen[0]!.headers.get(AUTHOR_SESSION_HEADER)).toBe('sess-42')
  })
})
