import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { applyEdits, globToRegExp, matchFiles, numbered } from '../src/tools/files.js'
import { runTool, ToolError, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #813: the file tools agents already know (Read/Edit/MultiEdit/Write/Glob/Grep),
// over the model store through the backend's routes. Every write is one revision.

type Seen = { method: string; path: string; query: string; body: unknown }

const V1 = 'a'.repeat(40)
const V2 = 'b'.repeat(40)
const MAIN = 'width = 10;\ncube(width);\n'
const PARTS = 'module bar() { cube(1); }\nmodule baz() { bar(); }\n'

function record(version: string, slug = 'plate') {
  return { slug, name: slug, origin: 'user', version, has_thumbnail: false, has_readme: false, updated_at: 'now' }
}

function textResponse(body: string) {
  return new Response(body, { headers: { 'content-type': 'text/plain' } })
}

/** A tiny model store: `files` per slug, a revision that moves on every write. */
function store(
  models: Record<string, Record<string, string>> = { plate: { 'model.scad': MAIN, 'parts.scad': PARTS } },
  beforeWrite: (versions: Record<string, string>) => void = () => {},
) {
  const seen: Seen[] = []
  const versions: Record<string, string> = Object.fromEntries(Object.keys(models).map((s) => [s, V1]))
  const client = createBackendClient(BACKEND, async (request) => {
    const r = request as Request
    const raw = await r.text()
    const url = new URL(r.url)
    const path = decodeURIComponent(url.pathname)
    const body = raw ? JSON.parse(raw) : undefined
    seen.push({ method: r.method, path, query: url.search, body })
    if (r.method === 'GET' && path === '/api/v1/models') return Response.json(Object.keys(models).map((s) => record(versions[s]!, s)))
    const m = /^\/api\/v1\/models\/([^/]+)(\/.*)?$/.exec(path)
    const slug = m?.[1] ?? ''
    const rest = m?.[2] ?? ''
    const files = models[slug]
    if (!files) return Response.json({ detail: `no model named '${slug}'` }, { status: 404 })
    if (r.method === 'GET' && rest === '') return Response.json(record(versions[slug]!, slug))
    if (r.method === 'GET' && rest === '/files') {
      return Response.json(
        Object.keys(files)
          .filter((n) => n.endsWith('.scad'))
          .map((name) => ({ name, size: files[name]!.length, main: name === 'model.scad' })),
      )
    }
    const file = /^\/files\/(.+)$/.exec(rest)?.[1]
    if (r.method === 'GET' && file !== undefined) {
      return files[file] === undefined ? Response.json({ detail: `no file '${file}'` }, { status: 404 }) : textResponse(files[file])
    }
    const written = (name: string, content: string) => {
      beforeWrite(versions)
      const b = body as { base?: string | null }
      if (b.base && !versions[slug]!.startsWith(b.base)) {
        return Response.json({ detail: 'moved on', base: b.base, current: versions[slug] }, { status: 409 })
      }
      if (files[name] === content) return Response.json(record(versions[slug]!, slug))
      files[name] = content
      versions[slug] = versions[slug] === V1 ? V2 : 'c'.repeat(40)
      return Response.json(record(versions[slug]!, slug))
    }
    if (r.method === 'PUT' && rest === '/source') return written('model.scad', (body as { source: string }).source)
    if (r.method === 'PUT' && file !== undefined) return written(file, (body as { content: string }).content)
    if (r.method === 'GET' && rest === `/versions/${V1}/source`) return textResponse('cube(1);\n')
    const diff = /^\/versions\/([0-9a-f]+)\/diff$/.exec(rest)
    if (r.method === 'GET' && diff) {
      return Response.json({ slug, base: V1, head: diff[1], files: [], patch: '--- a/model.scad\n+++ b/model.scad\n@@ -1 +1 @@\n-width = 10;\n+width = 12;\n' })
    }
    return Response.json({ detail: 'unexpected' }, { status: 500 })
  })
  return { client, seen, models, versions }
}

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(client: ReturnType<typeof store>['client']): ToolContext {
  return {
    ...services({ backend: client }),
    principal: { id: 'token:t1', kind: 'bearer', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
  }
}

function payload(result: Awaited<ReturnType<typeof runTool>>): Record<string, unknown> {
  return firstText(result) as Record<string, unknown>
}

describe('helpers', () => {
  it('numbers lines like cat -n and pages them', () => {
    expect(numbered('a\nb\nc\n', 1, 2000)).toEqual({ text: '     1\ta\n     2\tb\n     3\tc', from: 1, to: 3, total: 3 })
    expect(numbered('a\nb\nc', 2, 1)).toEqual({ text: '     2\tb', from: 2, to: 2, total: 3 })
    expect(numbered('', 1, 10).total).toBe(0)
  })

  it('replaces exactly, refusing a missing or ambiguous old_string', () => {
    expect(applyEdits('a $& a', [{ old_string: '$&', new_string: '$1' }])).toBe('a $1 a')
    expect(applyEdits('x x', [{ old_string: 'x', new_string: 'y', replace_all: true }])).toBe('y y')
    expect(() => applyEdits('x x', [{ old_string: 'x', new_string: 'y' }])).toThrow(/2 matches/)
    expect(() => applyEdits('x', [{ old_string: 'z', new_string: 'y' }])).toThrow(/not found/)
    expect(() => applyEdits('x', [{ old_string: 'x', new_string: 'x' }])).toThrow(/the same/)
    // All or nothing, in order: the second edit sees the first's result.
    expect(applyEdits('ab', [{ old_string: 'a', new_string: 'c' }, { old_string: 'cb', new_string: 'd' }])).toBe('d')
    expect(() => applyEdits('ab', [{ old_string: 'a', new_string: 'c' }, { old_string: 'a', new_string: 'd' }])).toThrow(
      ToolError,
    )
  })

  it('matches globs', () => {
    expect(globToRegExp('*.scad').test('parts.scad')).toBe(true)
    expect(globToRegExp('*.scad').test('README.md')).toBe(false)
    expect(globToRegExp('**/*.{scad,md}').test('README.md')).toBe(true)
    expect(globToRegExp('p?rts.scad').test('parts.scad')).toBe(true)
  })
})

describe('read_file', () => {
  it('returns numbered lines and the revision they were read at', async () => {
    const { client, seen } = store()
    const out = firstText(await runTool(tool('read_file'), { slug: 'plate', file_path: 'model.scad' }, ctx(client)))
    expect(out).toContain('     1\twidth = 10;')
    expect(out).toContain('     2\tcube(width);')
    expect(out).toContain(V1)
    // The revision is read before the file, so a write in between makes it older, never newer.
    expect(seen.map((s) => `${s.method} ${s.path}`)).toEqual(['GET /api/v1/models/plate', 'GET /api/v1/models/plate/files/model.scad'])
  })

  it('pages with offset and limit', async () => {
    const { client } = store()
    const out = firstText(await runTool(tool('read_file'), { slug: 'plate', file_path: 'parts.scad', offset: 2, limit: 1 }, ctx(client)))
    expect(out).toContain('     2\tmodule baz()')
    expect(out).not.toContain('     1\t')
    expect(out).toContain('lines 2-2 of 2')
  })

  it('reads model.scad at an earlier revision', async () => {
    const { client } = store()
    const out = firstText(await runTool(tool('read_file'), { slug: 'plate', file_path: 'model.scad', version: V1 }, ctx(client)))
    expect(out).toContain('     1\tcube(1);')
  })
})

describe('edit_file', () => {
  it('changes one line as one revision and returns the diff', async () => {
    const { client, seen, models } = store()
    const out = payload(
      await runTool(tool('edit_file'), { slug: 'plate', file_path: 'model.scad', old_string: 'width = 10;', new_string: 'width = 12;', message: 'Wider' }, ctx(client)),
    )
    expect(models.plate!['model.scad']).toBe('width = 12;\ncube(width);\n')
    expect(out).toMatchObject({ status: 'written', revision: V2, previous: V1 })
    expect(out.diff).toContain('+width = 12;')
    expect(out.content).toBeUndefined()
    const put = seen.find((s) => s.method === 'PUT')!
    expect(put.path).toBe('/api/v1/models/plate/source')
    // The read revision guards the write, so nothing lands between the read and the write unseen.
    expect(put.body).toEqual({ source: 'width = 12;\ncube(width);\n', message: 'Wider', base: V1, force: false })
  })

  it('writes a sibling file through its own route, with the base', async () => {
    const { client, seen, models } = store()
    await runTool(tool('edit_file'), { slug: 'plate', file_path: 'parts.scad', old_string: 'cube(1)', new_string: 'cube(2)' }, ctx(client))
    expect(models.plate!['parts.scad']).toContain('cube(2)')
    const put = seen.find((s) => s.method === 'PUT')!
    expect(put.path).toBe('/api/v1/models/plate/files/parts.scad')
    expect(put.body).toEqual({ content: PARTS.replace('cube(1)', 'cube(2)'), message: null, base: V1 })
  })

  it('returns the whole file when asked', async () => {
    const { client } = store()
    const out = payload(
      await runTool(tool('edit_file'), { slug: 'plate', file_path: 'model.scad', old_string: '10', new_string: '11', response: 'full' }, ctx(client)),
    )
    expect(out.content).toBe('width = 11;\ncube(width);\n')
  })

  it('refuses a stale base with a conflict and writes nothing', async () => {
    const { client, seen, models } = store()
    const result = await runTool(
      tool('edit_file'),
      { slug: 'plate', file_path: 'model.scad', old_string: '10', new_string: '11', base: 'd'.repeat(40) },
      ctx(client),
    )
    expect(result.isError).toBe(true)
    expect(payload(result)).toMatchObject({ status: 'conflict', current: V1 })
    expect(seen.some((s) => s.method === 'PUT')).toBe(false)
    expect(models.plate!['model.scad']).toBe(MAIN)
  })

  it('answers a conflict the backend finds under its lock the same way', async () => {
    // The model moves on between this call's read and its write.
    const { client, models } = store(undefined, (versions) => {
      versions.plate = 'e'.repeat(40)
    })
    const result = await runTool(tool('edit_file'), { slug: 'plate', file_path: 'model.scad', old_string: '10', new_string: '11' }, ctx(client))
    expect(result.isError).toBe(true)
    expect(payload(result)).toMatchObject({ status: 'conflict', current: 'e'.repeat(40) })
    expect(models.plate!['model.scad']).toBe(MAIN)
  })

  it('refuses an ambiguous old_string before writing', async () => {
    const { client, seen } = store({ plate: { 'model.scad': 'x\nx\n' } })
    const result = await runTool(tool('edit_file'), { slug: 'plate', file_path: 'model.scad', old_string: 'x', new_string: 'y' }, ctx(client))
    expect(result.isError).toBe(true)
    expect(firstText(result)).toMatch(/2 matches/)
    expect(seen.some((s) => s.method === 'PUT')).toBe(false)
  })

  it('writes only .scad files at the top of the model', async () => {
    const { client, seen } = store()
    for (const file_path of ['README.md', '../other/model.scad', 'sub/x.scad', 'model.json']) {
      const result = await runTool(tool('edit_file'), { slug: 'plate', file_path, old_string: 'a', new_string: 'b' }, ctx(client))
      expect(result.isError, file_path).toBe(true)
    }
    expect(seen).toEqual([])
  })
})

describe('multi_edit', () => {
  it('applies every edit as one revision, or none', async () => {
    const { client, seen, models } = store()
    await runTool(
      tool('multi_edit'),
      {
        slug: 'plate',
        file_path: 'model.scad',
        edits: [
          { old_string: 'width = 10;', new_string: 'w = 10;' },
          { old_string: 'cube(width)', new_string: 'cube(w)' },
        ],
      },
      ctx(client),
    )
    expect(models.plate!['model.scad']).toBe('w = 10;\ncube(w);\n')
    expect(seen.filter((s) => s.method === 'PUT')).toHaveLength(1)

    const failed = await runTool(
      tool('multi_edit'),
      { slug: 'plate', file_path: 'model.scad', edits: [{ old_string: 'w = 10;', new_string: 'w = 9;' }, { old_string: 'nope', new_string: 'x' }] },
      ctx(client),
    )
    expect(failed.isError).toBe(true)
    expect(firstText(failed)).toMatch(/edit 2/)
    expect(models.plate!['model.scad']).toBe('w = 10;\ncube(w);\n')
  })
})

describe('write_file', () => {
  it('creates a sibling file', async () => {
    const { client, models } = store()
    const out = payload(await runTool(tool('write_file'), { slug: 'plate', file_path: 'new.scad', content: 'module n() {}\n' }, ctx(client)))
    expect(models.plate!['new.scad']).toBe('module n() {}\n')
    expect(out).toMatchObject({ status: 'written', revision: V2 })
  })

  it('reports an identical write as unchanged, with no diff of an older revision', async () => {
    const { client, seen } = store()
    // The backend commits nothing for an identical write and answers the same revision.
    const out = payload(await runTool(tool('write_file'), { slug: 'plate', file_path: 'model.scad', content: MAIN, base: V1 }, ctx(client)))
    expect(out).toMatchObject({ status: 'unchanged', revision: V1 })
    // Without a base too: the revision before the write is read first.
    const again = payload(await runTool(tool('write_file'), { slug: 'plate', file_path: 'model.scad', content: MAIN }, ctx(client)))
    expect(again).toMatchObject({ status: 'unchanged', revision: V1 })
    expect(seen.some((s) => s.path.endsWith('/diff'))).toBe(false)
  })
})

describe('glob', () => {
  it("lists a model's files that match", async () => {
    const { client } = store()
    expect(firstText(await runTool(tool('glob'), { slug: 'plate', pattern: '*.scad' }, ctx(client)))).toContain('model.scad\nparts.scad')
    expect(firstText(await runTool(tool('glob'), { slug: 'plate', pattern: 'p*' }, ctx(client)))).not.toContain('model.scad')
  })
})

describe('grep', () => {
  const models = {
    plate: { 'model.scad': 'include <parts.scad>\nfill();\n', 'parts.scad': 'module fill() {}\n' },
    sign: { 'model.scad': 'text("hi");\nFILL();\n' },
  }

  it('finds the files that match across every model', async () => {
    const { client } = store(structuredClone(models))
    const out = firstText(await runTool(tool('grep'), { pattern: 'fill\\(' }, ctx(client)))
    expect(out).toContain('plate/model.scad')
    expect(out).toContain('plate/parts.scad')
    expect(out).not.toContain('sign/model.scad')
    const insensitive = firstText(await runTool(tool('grep'), { pattern: 'fill\\(\\)', '-i': true }, ctx(client)))
    expect(insensitive).toContain('sign/model.scad')
  })

  it('shows matching lines with numbers and context in one model', async () => {
    const { client, seen } = store(structuredClone(models))
    const out = firstText(
      await runTool(tool('grep'), { slug: 'plate', pattern: 'fill', output_mode: 'content', glob: 'model.scad', '-B': 1 }, ctx(client)),
    )
    expect(out).toContain('plate/model.scad-1-include <parts.scad>')
    expect(out).toContain('plate/model.scad:2:fill();')
    expect(seen.some((s) => s.path.includes('/sign'))).toBe(false)
    expect(seen.some((s) => s.path.endsWith('/parts.scad'))).toBe(false)
  })

  it('counts', async () => {
    const { client } = store(structuredClone(models))
    expect(firstText(await runTool(tool('grep'), { pattern: 'fill', output_mode: 'count', '-i': true }, ctx(client)))).toContain('sign/model.scad:1')
  })

  it('refuses a bad pattern, and stops a runaway one', async () => {
    const { client } = store(structuredClone(models))
    expect((await runTool(tool('grep'), { pattern: '(' }, ctx(client))).isError).toBe(true)
    const runaway = matchFiles(
      [{ name: 'plate/model.scad', text: `${'a'.repeat(40)}!\n` }],
      { pattern: '(a+)+$', ignoreCase: false, mode: 'content', before: 0, after: 0, lineNumbers: true },
      { timeoutMs: 200 },
    )
    await expect(runaway).rejects.toThrow(/took longer/)
  }, 10_000)

  it('are all read-tier except the writes', () => {
    expect(['read_file', 'glob', 'grep'].map((n) => tool(n).risk)).toEqual(['read', 'read', 'read'])
    expect(['edit_file', 'multi_edit', 'write_file'].map((n) => tool(n).risk)).toEqual(['write', 'write', 'write'])
  })
})
