import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { applyEdits, globMatcher, matchFiles, numbered } from '../src/tools/files.js'
import { runTool, ToolError, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #813: the file tools agents already know (Read/Edit/MultiEdit/Write/Glob/Grep),
// over the model store through the backend's routes. Every write is one revision.

type Seen = { method: string; path: string; query: string; body: unknown; key: string | null }

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

/**
 * A tiny model store: `files` per slug, a revision that moves on every write. Writes are
 * commands, as on the backend (#1054): refused without an Idempotency-Key, and with
 * `accepted` answered 202 with an operation that GET /operations/{id} then reports done.
 */
type Extras = {
  /** Files per pinned library name, served as the checkout `plate` pins. */
  libraries?: Record<string, Record<string, string>>
  /** Each file as it was at V1, for a read at that revision. */
  atV1?: Record<string, string>
  /** What POST /lsp/diagnostics answers. */
  lsp?: () => Response
  /** Answer GET /tree as cut at the backend's cap. */
  treeCut?: boolean
}

function store(
  models: Record<string, Record<string, string>> = { plate: { 'model.scad': MAIN, 'parts.scad': PARTS } },
  beforeWrite: (versions: Record<string, string>) => void = () => {},
  accepted = false,
  extras: Extras = {},
) {
  const seen: Seen[] = []
  const versions: Record<string, string> = Object.fromEntries(Object.keys(models).map((s) => [s, V1]))
  const operations: Record<string, unknown> = {}
  const client = createBackendClient(BACKEND, async (request) => {
    const r = request as Request
    const raw = await r.text()
    const url = new URL(r.url)
    const path = decodeURIComponent(url.pathname)
    const body = raw ? JSON.parse(raw) : undefined
    const key = r.headers.get('Idempotency-Key')
    seen.push({ method: r.method, path, query: url.search, body, key })
    if (r.method === 'GET' && path === '/api/v1/models') return Response.json(Object.keys(models).map((s) => record(versions[s]!, s)))
    if (r.method === 'POST' && path === '/api/v1/lsp/diagnostics') {
      return extras.lsp ? extras.lsp() : Response.json({ available: true, diagnostics: [] })
    }
    const op = /^\/api\/v1\/operations\/([^/]+)$/.exec(path)?.[1]
    if (r.method === 'GET' && op !== undefined) return Response.json({ id: op, status: 'succeeded', result: operations[op] })
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
    const listing = (all: Record<string, string>) =>
      Response.json({
        files: Object.keys(all)
          .sort()
          .map((p) => ({ path: p, size: all[p]!.length })),
        truncated: extras.treeCut ?? false,
      })
    if (r.method === 'GET' && rest === '/tree') return listing(files)
    const lib = /^\/libraries\/([^/]+)\/files(?:\/(.+))?$/.exec(rest)
    if (r.method === 'GET' && lib) {
      const checkout = extras.libraries?.[lib[1]!]
      if (!checkout) return Response.json({ detail: `does not pin ${lib[1]}` }, { status: 404 })
      if (lib[2] === undefined) return listing(checkout)
      const found = checkout[lib[2]]
      return found === undefined ? Response.json({ detail: 'no file' }, { status: 404 }) : textResponse(found)
    }
    const then = new RegExp(`^/versions/${V1}/files/(.+)$`).exec(rest)?.[1]
    if (r.method === 'GET' && then !== undefined) {
      const old = extras.atV1?.[then]
      return old === undefined ? Response.json({ detail: 'no file' }, { status: 404 }) : textResponse(old)
    }
    const file = /^\/files\/(.+)$/.exec(rest)?.[1]
    if (r.method === 'GET' && file !== undefined) {
      if (file.endsWith('.png')) return Response.json({ detail: `'${file}' is not UTF-8 text` }, { status: 415 })
      return files[file] === undefined ? Response.json({ detail: `no file '${file}'` }, { status: 404 }) : textResponse(files[file])
    }
    const written = (name: string, content: string) => {
      if (!key) return Response.json({ detail: 'This request needs an Idempotency-Key header' }, { status: 428 })
      beforeWrite(versions)
      const b = body as { base?: string | null }
      if (b.base && !versions[slug]!.startsWith(b.base)) {
        return Response.json({ detail: 'moved on', base: b.base, current: versions[slug] }, { status: 409 })
      }
      if (files[name] !== content) {
        files[name] = content
        versions[slug] = versions[slug] === V1 ? V2 : 'c'.repeat(40)
      }
      if (!accepted) return Response.json(record(versions[slug]!, slug))
      operations[`op-${key}`] = record(versions[slug]!, slug)
      return Response.json({ id: `op-${key}`, status: 'running' }, { status: 202 })
    }
    if (r.method === 'PUT' && rest === '/source') return written('model.scad', (body as { source: string }).source)
    if (r.method === 'PUT' && rest === '/readme') return written('README.md', (body as { content: string }).content)
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
    const m = (glob: string, name: string) => globMatcher(glob)(name)
    expect(m('*.scad', 'parts.scad')).toBe(true)
    expect(m('*.scad', 'README.md')).toBe(false)
    expect(m('**/*.{scad,md}', 'README.md')).toBe(true)
    expect(m('p?rts.scad', 'parts.scad')).toBe(true)
    expect(m('{a,b{c,d}}.scad', 'bd.scad')).toBe(true)
    expect(m('*', 'a/b')).toBe(false)
    expect(m('**', 'a/b')).toBe(true)
    expect(m('a/**/b', 'a/b')).toBe(true)
    expect(m('a/**/b', 'a/x/y/b')).toBe(true)
    expect(m('[a-c]x', 'bx')).toBe(true)
    expect(m('[!p]*.scad', 'parts.scad')).toBe(false)
    // A `]` first in a class is a member; outside a group `,` and `}` are literal.
    expect(m('[]a]x', ']x')).toBe(true)
    expect(m('a,b}', 'a,b}')).toBe(true)
    expect(() => globMatcher('{a,b')).toThrow(ToolError)
  })

  it('matches in time linear in the name, whatever the glob (#1069 review)', () => {
    // Each of these held a RegExp built from the glob for good, on the service's own thread.
    const started = performance.now()
    expect(globMatcher('**'.repeat(99) + 'x')('model.scad')).toBe(false)
    expect(globMatcher('*a'.repeat(30) + 'b')(`${'a'.repeat(90)}.scad`)).toBe(false)
    expect(globMatcher('*a'.repeat(30))('a'.repeat(90))).toBe(true)
    expect(performance.now() - started).toBeLessThan(1000)
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

  it('sends the write as a command, keyed, and follows its 202 to the revision and diff', async () => {
    const { client, seen } = store(undefined, undefined, true)
    const out = payload(
      await runTool(tool('edit_file'), { slug: 'plate', file_path: 'parts.scad', old_string: 'cube(1)', new_string: 'cube(2)' }, ctx(client)),
    )
    expect(out).toMatchObject({ status: 'written', revision: V2, previous: V1 })
    const put = seen.find((s) => s.method === 'PUT')!
    expect(put.key).toMatch(/^[0-9a-f]{32}$/)
    expect(seen.some((s) => s.path === `/api/v1/operations/op-${put.key}`)).toBe(true)
  })

  it('hands back a write still running past the follow window, to follow with get_operation', async () => {
    const { client, seen } = store(undefined, undefined, true)
    const result = await runTool(
      tool('write_file'),
      { slug: 'plate', file_path: 'model.scad', content: 'cube(3);\n' },
      { ...ctx(client), commandFollowMs: 0 },
    )
    const put = seen.find((s) => s.method === 'PUT')!
    expect(payload(result)).toMatchObject({ status: 'running', operation_id: `op-${put.key}` })
    expect(seen.some((s) => s.path.endsWith('/diff'))).toBe(false)
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

  it('writes only README.md and .scad files at the top of the model', async () => {
    const { client, seen } = store()
    for (const file_path of ['../other/model.scad', 'sub/x.scad', 'model.json', 'ui/index.html']) {
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

// #1067: past the .scad files.
describe('the whole model directory', () => {
  const directory = () => ({
    plate: {
      'model.scad': MAIN,
      'parts.scad': PARTS,
      'README.md': '# Plate\nA flat plate.\n',
      'model.json': '{"name": "Plate"}\n',
      'ui/index.html': '<p>plate</p>\n',
      'images/cover.png': 'PNG',
    },
  })

  it('reads any file as it was at an earlier revision', async () => {
    const { client, seen } = store(undefined, undefined, false, { atV1: { 'parts.scad': 'module bar() {}\n' } })
    const out = firstText(await runTool(tool('read_file'), { slug: 'plate', file_path: 'parts.scad', version: V1 }, ctx(client)))
    expect(out).toContain('     1\tmodule bar() {}')
    expect(out).toContain(`read at earlier revision ${V1}`)
    expect(seen.map((s) => s.path)).toEqual([`/api/v1/models/plate/versions/${V1}/files/parts.scad`])
  })

  it('globs every path in the directory, `*` stopping at `/`', async () => {
    const { client } = store(directory())
    const all = firstText(await runTool(tool('glob'), { slug: 'plate', pattern: '**' }, ctx(client))) as string
    expect(all.split('\n')).toEqual(['README.md', 'images/cover.png', 'model.json', 'model.scad', 'parts.scad', 'ui/index.html'])
    expect(firstText(await runTool(tool('glob'), { slug: 'plate', pattern: '*.scad' }, ctx(client)))).toBe('model.scad\nparts.scad')
    expect(firstText(await runTool(tool('glob'), { slug: 'plate', pattern: 'ui/**' }, ctx(client)))).toBe('ui/index.html')
  })

  it('says when the listing was cut at the backend cap', async () => {
    const { client } = store(directory(), undefined, false, { treeCut: true })
    expect(firstText(await runTool(tool('glob'), { slug: 'plate', pattern: '**' }, ctx(client)))).toMatch(/only the first ones/)
  })

  it('greps every file a glob names, skipping what is not text', async () => {
    const { client, seen } = store(directory())
    const out = firstText(await runTool(tool('grep'), { slug: 'plate', pattern: 'plate', glob: '**', '-i': true }, ctx(client))) as string
    expect(out.split('\n')).toEqual(['plate/README.md', 'plate/model.json', 'plate/ui/index.html'])
    expect(seen.some((s) => s.path.endsWith('/images/cover.png'))).toBe(true)
    // Without a glob it is the .scad files, as before.
    const scad = firstText(await runTool(tool('grep'), { slug: 'plate', pattern: 'Plate' }, ctx(client)))
    expect(scad).toMatch(/^no matches in 2 file/)
  })
})

describe('grep in a pinned library', () => {
  const libraries = {
    BOSL2: {
      'std.scad': 'include <shapes3d.scad>\n',
      'shapes3d.scad': 'module cuboid(size, rounding) {}\n',
      'tests/test_shapes3d.scad': 'cuboid(10);\n',
      'README.md': 'cuboid docs\n',
    },
  }

  it("searches the library's .scad files, named as `use <…>` names them", async () => {
    const { client, seen } = store(undefined, undefined, false, { libraries })
    const out = firstText(
      await runTool(tool('grep'), { slug: 'plate', library: 'BOSL2', pattern: 'module cuboid', output_mode: 'content' }, ctx(client)),
    )
    expect(out).toBe('BOSL2/shapes3d.scad:1:module cuboid(size, rounding) {}')
    expect(seen.some((s) => s.path === '/api/v1/models/plate/libraries/BOSL2/files/README.md')).toBe(false)
    const tests = firstText(await runTool(tool('grep'), { slug: 'plate', library: 'BOSL2', pattern: 'cuboid', glob: 'tests/**' }, ctx(client)))
    expect(tests).toBe('BOSL2/tests/test_shapes3d.scad')
  })

  it('needs the model that pins it, and reports a library it does not pin', async () => {
    const { client } = store(undefined, undefined, false, { libraries })
    const noSlug = await runTool(tool('grep'), { library: 'BOSL2', pattern: 'x' }, ctx(client))
    expect(noSlug.isError).toBe(true)
    expect(firstText(noSlug)).toMatch(/needs the `slug`/)
    expect((await runTool(tool('grep'), { slug: 'plate', library: 'MCAD', pattern: 'x' }, ctx(client))).isError).toBe(true)
  })
})

describe('writing README.md and model.json', () => {
  it('edits README.md through its route, against the base it read', async () => {
    const { client, seen, models } = store({ plate: { 'model.scad': MAIN, 'README.md': '# Plate\n' } })
    const out = payload(
      await runTool(tool('edit_file'), { slug: 'plate', file_path: 'README.md', old_string: '# Plate', new_string: '# Flat plate', message: 'Retitle' }, ctx(client)),
    )
    expect(models.plate!['README.md']).toBe('# Flat plate\n')
    expect(out).toMatchObject({ status: 'written', revision: V2 })
    expect(out).not.toHaveProperty('diagnostics')
    const put = seen.find((s) => s.method === 'PUT')!
    expect(put.path).toBe('/api/v1/models/plate/readme')
    expect(put.body).toEqual({ content: '# Flat plate\n', message: 'Retitle', base: V1 })
    expect(seen.some((s) => s.path === '/api/v1/lsp/diagnostics')).toBe(false)
  })

  it('refuses model.json, which update_model writes', async () => {
    const { client, seen } = store()
    const result = await runTool(tool('write_file'), { slug: 'plate', file_path: 'model.json', content: '{}' }, ctx(client))
    expect(result.isError).toBe(true)
    expect(firstText(result)).toMatch(/update_model/)
    expect(seen).toEqual([])
  })
})

describe("a sibling write's diagnostics", () => {
  const diagnostic = { message: 'syntax error', severity: 'error', start: { line: 0, character: 3 }, end: { line: 0, character: 4 } }

  it("returns openscad-lsp's diagnostics of the file it wrote", async () => {
    const { client, seen } = store(undefined, undefined, false, { lsp: () => Response.json({ available: true, diagnostics: [diagnostic] }) })
    const out = payload(await runTool(tool('write_file'), { slug: 'plate', file_path: 'new.scad', content: 'mod(\n' }, ctx(client)))
    expect(out).toMatchObject({ status: 'written', diagnostics: [diagnostic] })
    expect(seen.find((s) => s.path === '/api/v1/lsp/diagnostics')!.body).toEqual({ source: 'mod(\n', slug: 'plate' })
  })

  it('leaves model.scad to its own parse check', async () => {
    const { client, seen } = store()
    await runTool(tool('write_file'), { slug: 'plate', file_path: 'model.scad', content: 'cube(3);\n' }, ctx(client))
    expect(seen.some((s) => s.path === '/api/v1/lsp/diagnostics')).toBe(false)
  })

  it('reports the write as written when the check cannot run', async () => {
    const busy = store(undefined, undefined, false, { lsp: () => Response.json({ detail: 'busy' }, { status: 503 }) })
    const out = payload(await runTool(tool('write_file'), { slug: 'plate', file_path: 'new.scad', content: 'module n() {}\n' }, ctx(busy.client)))
    expect(out).toMatchObject({ status: 'written', diagnostics_note: expect.stringMatching(/answered 503/) })

    const none = store(undefined, undefined, false, { lsp: () => Response.json({ available: false, diagnostics: [] }) })
    const quiet = payload(await runTool(tool('write_file'), { slug: 'plate', file_path: 'new.scad', content: 'module n() {}\n' }, ctx(none.client)))
    expect(quiet).toMatchObject({ status: 'written', diagnostics_note: expect.stringMatching(/no openscad-lsp/) })

    const broken = store(undefined, undefined, false, {
      lsp: () => {
        throw new TypeError('fetch failed')
      },
    })
    const lost = payload(await runTool(tool('write_file'), { slug: 'plate', file_path: 'new.scad', content: 'module n() {}\n' }, ctx(broken.client)))
    expect(lost).toMatchObject({ status: 'written', diagnostics_note: expect.stringMatching(/fetch failed/) })
    expect(broken.models.plate!['new.scad']).toBe('module n() {}\n')
  })
})
