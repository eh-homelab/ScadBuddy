import { readFile } from 'node:fs/promises'
import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { matchUri } from '../src/resources/catalog.js'
import { stripFrontmatter } from '../src/tools/guide.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #252's looking tools: the language server's diagnostics as data, the
// per-colour breakdown image, and the authoring guide (tool and resource).

type Seen = { method: string; path: string; search: string; body: unknown }

function backend(answer: (seen: Seen) => Response) {
  const seen: Seen[] = []
  const client = createBackendClient(BACKEND, async (request) => {
    const r = request as Request
    const url = new URL(r.url)
    const raw = await r.text()
    let body: unknown = raw || undefined
    try {
      body = raw ? JSON.parse(raw) : undefined
    } catch {
      // not JSON
    }
    const entry = { method: r.method, path: url.pathname, search: url.search, body }
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

function ctx(client: ReturnType<typeof backend>['client']): ToolContext {
  return {
    ...services({ backend: client }),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('read') },
    progress: async () => {},
    signal: new AbortController().signal,
  }
}

const DIAGNOSTICS = {
  available: true,
  diagnostics: [{ line: 2, column: 12, end_line: 2, end_column: 13, severity: 'error', message: 'syntax error' }],
}

describe('get_lsp_diagnostics', () => {
  it('checks unsaved source as given', async () => {
    const { client, seen } = backend(() => Response.json(DIAGNOSTICS))
    const result = await runTool(tool('get_lsp_diagnostics'), { source: 'cube([1, 2 3]);' }, ctx(client))
    expect(firstText(result)).toEqual(DIAGNOSTICS)
    expect(seen.map((s) => [s.method, s.path, s.body])).toEqual([
      ['POST', '/api/v1/lsp/diagnostics', { source: 'cube([1, 2 3]);', slug: null }],
    ])
  })

  it("reads a model's saved source when only the slug is given", async () => {
    const { client, seen } = backend((s) =>
      s.method === 'GET' ? new Response('cube(1);', { headers: { 'content-type': 'text/plain' } }) : Response.json(DIAGNOSTICS),
    )
    await runTool(tool('get_lsp_diagnostics'), { slug: 'box' }, ctx(client))
    expect(seen.map((s) => [s.method, s.path])).toEqual([
      ['GET', '/api/v1/models/box/source'],
      ['POST', '/api/v1/lsp/diagnostics'],
    ])
    expect(seen[1]!.body).toEqual({ source: 'cube(1);', slug: 'box' })
  })

  it('needs one of them', async () => {
    const { client, seen } = backend(() => Response.json(DIAGNOSTICS))
    const result = await runTool(tool('get_lsp_diagnostics'), {}, ctx(client))
    expect(result.isError).toBe(true)
    expect(seen).toEqual([])
  })
})

describe('get_render_colours', () => {
  const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

  it('answers the legend, row by row in extruder order, then the image', async () => {
    const { client, seen } = backend(
      () =>
        new Response(PNG, {
          headers: { 'content-type': 'image/png', 'x-scadbuddy-colours': '#FF0000,#00FF00,#0000FF,not-a-colour' },
        }),
    )
    const result = await runTool(tool('get_render_colours'), { job_id: 'a'.repeat(32), size: 128 }, ctx(client))
    expect(seen[0]!.path).toBe(`/api/v1/jobs/${'a'.repeat(32)}/colours.png`)
    expect(new URLSearchParams(seen[0]!.search).get('view')).toBe('iso')
    expect(firstText(result)).toEqual({
      view: 'iso',
      tiles: [
        { colour: '#FF0000', extruder_order: 1, row: 1, column: 1 },
        { colour: '#00FF00', extruder_order: 2, row: 1, column: 2 },
        { colour: '#0000FF', extruder_order: 3, row: 2, column: 1 },
      ],
    })
    const picture = result.content.find((c) => c.type === 'image') as { data: string; mimeType: string }
    expect(picture.mimeType).toBe('image/png')
    expect(Buffer.from(picture.data, 'base64')).toEqual(Buffer.from(PNG))
  })

  it('keeps the legend when the image is too large to inline and comes back as a link', async () => {
    const { client } = backend(
      () => new Response(PNG, { headers: { 'content-type': 'image/png', 'x-scadbuddy-colours': '#FF0000,#00FF00' } }),
    )
    const result = await runTool(
      tool('get_render_colours'),
      { job_id: 'c'.repeat(32) },
      { ...ctx(client), maxInlineBytes: PNG.length - 1 },
    )
    expect(firstText(result)).toMatchObject({
      tiles: [
        { colour: '#FF0000', row: 1, column: 1 },
        { colour: '#00FF00', row: 1, column: 2 },
      ],
    })
    expect(result.content.some((c) => c.type === 'resource_link')).toBe(true)
    expect(result.content.some((c) => c.type === 'image')).toBe(false)
  })

  it('passes a refusal through', async () => {
    const { client } = backend(() => Response.json({ detail: '17 colours is more than a breakdown draws (16)' }, { status: 422 }))
    const result = await runTool(tool('get_render_colours'), { job_id: 'b'.repeat(32) }, ctx(client))
    expect(result.isError).toBe(true)
    expect(String(firstText(result))).toContain('more than a breakdown draws')
  })
})

describe('the authoring guide (#252)', () => {
  it('is the authoring skill without its frontmatter', async () => {
    const { client } = backend(() => new Response(null, { status: 500 }))
    const result = await runTool(tool('get_authoring_guide'), {}, ctx(client))
    const skill = await readFile(new URL('../../plugins/scadbuddy/skills/authoring/SKILL.md', import.meta.url), 'utf8')
    const guide = String(firstText(result))
    expect(guide).toBe(stripFrontmatter(skill))
    expect(guide.startsWith('# Authoring ScadBuddy templates')).toBe(true)
    // The verified facts the issue names.
    expect(guide).toContain('Lobster Two')
    expect(guide).toContain('lazy-union')
  })

  it('is the resource scadbuddy://docs/authoring', () => {
    const match = matchUri('scadbuddy://docs/authoring')
    expect(match?.def.tool).toBe('get_authoring_guide')
    expect(match?.def.mimeType).toBe('text/markdown')
    expect(tool('get_authoring_guide').risk).toBe('read')
  })

  it('strips only a leading frontmatter block', () => {
    expect(stripFrontmatter('---\nname: x\n---\n\n# Title\n---\n')).toBe('# Title\n---\n')
    expect(stripFrontmatter('# No frontmatter\n')).toBe('# No frontmatter\n')
  })
})
