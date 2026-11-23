import { HttpResponse, delay, http } from 'msw'
import type { PackageReview } from '../../api/aiPlugins'
import type { HostedPlugin, HostedPluginDraftDiff, HostedPluginFile, HostedPluginVersion } from '../../api/hostedPlugins'
import { dropHostedPackage, packageNameTaken, setHostedLookup } from '../aiPlugins'

/**
 * The backend's `/api/v1/hosted-plugins` (#6932, `backend/scadbuddy/api/hosted_plugins.py`) for
 * the editor in Settings → Tools & plugins (#6937). The rules the UI depends on are kept: the
 * name rules and the package-name clash (409 `package-name-taken`), the path rules
 * (`hosted_plugins/rules.py`: under skills/, agents/ or commands/, no hidden segment, the
 * generated plugin.json refused, executables only under skills/), the caps, a publish that
 * checks each skill's SKILL.md frontmatter (422 `draft-invalid` with `problems`) and refuses
 * a draft equal to the latest version (409 `nothing-to-publish`). A published version is what
 * the agent's mock (`mocks/aiPlugins.ts`) installs on an Offer.
 *
 * Seeded: `print-notes`, version 1 published and never offered, with one skill in its draft.
 */

const BASE = '/api/v1/hosted-plugins'
const MAX_FILE_BYTES = 2 * 1024 * 1024
const MAX_FILES = 500
const MAX_TOTAL_BYTES = 20 * 1024 * 1024
const TOP_LEVEL = ['skills', 'agents', 'commands']
const MANIFEST = '.claude-plugin/plugin.json'
const NAME_RE = /^[a-z][a-z0-9-]{0,30}[a-z0-9]$/
const RESERVED = new Set(['scadbuddy', 'claude', 'anthropic', 'plugin', 'workspace', 'ide'])

interface DraftFile {
  bytes: Uint8Array
  sha256: string
  executable: boolean
  updated_at: string
}

interface Plugin {
  name: string
  description: string
  category: string | null
  tags: string[]
  files: Map<string, DraftFile>
  versions: (HostedPluginVersion & { description: string })[]
  created_at: string
  updated_at: string
}

const plugins = new Map<string, Plugin>()
const encoder = new TextEncoder()
const decoder = new TextDecoder()
let clock = Date.parse('2026-11-21T09:00:00Z')
const stamp = () => new Date((clock += 60_000)).toISOString()

/** A stand-in for a sha256: stable for the same bytes, 64 hex digits. */
function digest(bytes: Uint8Array): string {
  let h1 = 0x811c9dc5
  let h2 = 0x01000193
  for (const b of bytes) {
    h1 = Math.imul(h1 ^ b, 0x01000193) >>> 0
    h2 = Math.imul(h2 ^ b, 0x5bd1e995) >>> 0
  }
  return (h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0')).repeat(4)
}

const problem = (status: number, detail: string, reason: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ title: 'Refused', status, detail, reason, ...extra }, { status })

const notFound = (name: string) => problem(404, `no hosted plugin named "${name}"`, 'hosted-plugin-not-found')

function view(p: Plugin): HostedPlugin {
  return {
    name: p.name,
    description: p.description,
    category: p.category,
    tags: p.tags,
    latest_version: p.versions.at(-1)?.version ?? null,
    file_count: p.files.size,
    total_bytes: [...p.files.values()].reduce((n, f) => n + f.bytes.length, 0),
    created_at: p.created_at,
    updated_at: p.updated_at,
  }
}

function fileView(path: string, f: DraftFile): HostedPluginFile {
  return { path, sha256: f.sha256, size: f.bytes.length, executable: f.executable, updated_at: f.updated_at }
}

/** Why `path` is refused, or null (`rules.py` `check_path`, `check_executable`). */
function pathProblem(path: string, executable: boolean): string | null {
  if (path === '') return 'the path is empty'
  if (path.includes('\\')) return 'the path holds a backslash; separate folders with /'
  if (path.startsWith('/')) return "the path must be relative to the plugin's root"
  if (path === MANIFEST) return `${MANIFEST} is generated when the plugin is published`
  const segments = path.split('/')
  for (const segment of segments) {
    if (segment === '') return 'the path has an empty segment'
    if (segment === '..') return 'the path holds ".."'
    if (segment.startsWith('.')) return `"${segment}" starts with "."; no hidden files or folders`
  }
  if (segments.length < 2 || !TOP_LEVEL.includes(segments[0]!)) return 'a file must be under skills/, agents/ or commands/'
  if (executable && !path.startsWith('skills/')) return 'only a file under skills/ may be executable'
  return null
}

/** Why `path` cannot sit beside the others (`rules.py` `clash`), or null. */
function clash(path: string, others: string[]): string | null {
  for (const other of others) {
    if (other === path) continue
    if (other.startsWith(`${path}/`)) return `${path} is a folder of another file`
    if (path.startsWith(`${other}/`)) return `${other} is a file, so it cannot hold ${path}`
    if (other.toLowerCase() === path.toLowerCase()) return `${path} differs from ${other} only in case`
  }
  return null
}

function frontmatter(text: string): Record<string, string> | null {
  const match = /^---\n([\s\S]*?)\n---/.exec(text)
  if (!match) return null
  const fields: Record<string, string> = {}
  for (const line of match[1]!.split('\n')) {
    const at = line.indexOf(':')
    if (at > 0) fields[line.slice(0, at).trim()] = line.slice(at + 1).trim()
  }
  return fields
}

/** The publish's draft check (`bundle.problems`), in short. */
function draftProblems(p: Plugin): string[] {
  const found: string[] = []
  if (p.files.size === 0) found.push('the draft has no files')
  const skills = new Set([...p.files.keys()].filter((f) => f.startsWith('skills/')).map((f) => f.split('/')[1]!))
  for (const skill of skills) {
    const file = p.files.get(`skills/${skill}/SKILL.md`)
    if (!file) {
      found.push(`skills/${skill}/ has no SKILL.md`)
      continue
    }
    const fields = frontmatter(decoder.decode(file.bytes))
    if (!fields?.name || !fields.description) found.push(`skills/${skill}/SKILL.md: the frontmatter needs a name and a description`)
  }
  for (const [path, file] of p.files) {
    if (path.endsWith('.md') && decoder.decode(file.bytes).includes('!`')) {
      found.push(`${path}: runs a shell command through dynamic context injection (!\`...\`)`)
    }
  }
  return found
}

function listed(p: Plugin, version: number): Record<string, { sha256: string; size: number; executable: boolean }> {
  const manifest = encoder.encode(JSON.stringify({ name: p.name, description: p.description, version: `${version}.0.0` }))
  const files: Record<string, { sha256: string; size: number; executable: boolean }> = {
    [MANIFEST]: { sha256: digest(manifest), size: manifest.length, executable: false },
  }
  for (const path of [...p.files.keys()].sort()) {
    const f = p.files.get(path)!
    files[path] = { sha256: f.sha256, size: f.bytes.length, executable: f.executable }
  }
  return files
}

const withoutManifest = (files: Record<string, unknown>) => Object.fromEntries(Object.entries(files).filter(([p]) => p !== MANIFEST))

function diff(p: Plugin): HostedPluginDraftDiff {
  const base = p.versions.at(-1)
  const before = base ? (withoutManifest(base.files) as Record<string, { sha256: string }>) : {}
  const after = Object.fromEntries([...p.files].map(([path, f]) => [path, f.sha256]))
  return {
    base_version: base?.version ?? null,
    added: Object.keys(after).filter((f) => !(f in before)).sort(),
    removed: Object.keys(before).filter((f) => !(f in after)).sort(),
    changed: Object.keys(after).filter((f) => f in before && before[f]!.sha256 !== after[f]).sort(),
    manifest_changed: base !== undefined && base.description !== p.description,
  }
}

/** The review the agent would store for a version: what its files name. */
function review(p: Plugin, version: HostedPluginVersion & { description: string }): PackageReview {
  const paths = Object.keys(version.files)
  const named = (folder: string, pick: (rest: string[]) => string | null) =>
    [...new Set(paths.filter((f) => f.startsWith(`${folder}/`)).map((f) => pick(f.split('/').slice(1))))]
      .filter((n): n is string => n !== null)
      .map((n) => `${p.name}:${n}`)
      .sort()
  const md = (rest: string[]) => (rest.length === 1 && rest[0]!.endsWith('.md') ? rest[0]!.slice(0, -3) : null)
  return {
    name: p.name,
    description: version.description,
    version: `${version.version}.0.0`,
    skills: named('skills', (rest) => rest[0] ?? null),
    commands: named('commands', md),
    agents: named('agents', md),
    hooks: [],
    mcp_servers: [],
    files: paths.sort(),
    refused: [],
    described: {},
  }
}

setHostedLookup((name) => {
  const p = plugins.get(name)
  const latest = p?.versions.at(-1)
  return p && latest ? { latest: latest.version, review: review(p, latest) } : undefined
})

function put(p: Plugin, path: string, bytes: Uint8Array, executable = false): void {
  p.files.set(path, { bytes, sha256: digest(bytes), executable, updated_at: stamp() })
  p.updated_at = stamp()
}

function publish(p: Plugin): HostedPluginVersion & { description: string } {
  const number = (p.versions.at(-1)?.version ?? 0) + 1
  const files = listed(p, number)
  const version = {
    version: number,
    zip_sha256: digest(encoder.encode(JSON.stringify(files))),
    content_hash: `sha256:${digest(encoder.encode(JSON.stringify(Object.keys(files))))}`,
    files,
    published_at: stamp(),
    published_by: 'person:admin@example.com',
    description: p.description,
  }
  p.versions.push(version)
  return version
}

export const SEEDED_SKILL = '---\nname: print-notes\ndescription: Writes a short note about a finished print\n---\n\nWrite two lines about the print: what went well and what to change.\n'

export function reset(): void {
  plugins.clear()
  clock = Date.parse('2026-11-21T09:00:00Z')
  const seeded: Plugin = {
    name: 'print-notes',
    description: 'Notes about finished prints',
    category: null,
    tags: [],
    files: new Map(),
    versions: [],
    created_at: stamp(),
    updated_at: stamp(),
  }
  put(seeded, 'skills/print-notes/SKILL.md', encoder.encode(SEEDED_SKILL))
  publish(seeded)
  plugins.set(seeded.name, seeded)
}
reset()

const named = (params: Record<string, unknown>) => plugins.get(String(params.name))
/** The draft path from the URL: msw's `*` keeps the encoded segments. */
const pathOf = (params: Record<string, unknown>) => {
  const raw = params['0'] ?? params.path
  const joined = Array.isArray(raw) ? raw.join('/') : String(raw ?? '')
  return joined.split('/').map(decodeURIComponent).join('/')
}

export const handlers = [
  http.get(BASE, () => HttpResponse.json([...plugins.values()].map(view))),

  http.post(BASE, async ({ request }) => {
    const body = (await request.json()) as { name: string; description?: string; category?: string | null; tags?: string[] }
    const name = body.name ?? ''
    if (name === 'scadbuddy') return problem(409, `"${name}" is a plugin built into ScadBuddy`, 'package-name-taken')
    if (!NAME_RE.test(name) || name.includes('--')) {
      return problem(422, `"${name}" is not 2-32 lower-case letters, digits and single hyphens, starting with a letter and ending with a letter or digit`, 'name-refused')
    }
    if (RESERVED.has(name)) return problem(422, `"${name}" is reserved: it may clash with ScadBuddy's own plugin or tools`, 'name-refused')
    if (plugins.has(name)) return problem(409, `a hosted plugin named "${name}" exists`, 'hosted-plugin-exists')
    if (packageNameTaken(name)) return problem(409, `a plugin package named "${name}" is installed`, 'package-name-taken')
    const at = stamp()
    const p: Plugin = {
      name,
      description: body.description ?? '',
      category: body.category ?? null,
      tags: body.tags ?? [],
      files: new Map(),
      versions: [],
      created_at: at,
      updated_at: at,
    }
    plugins.set(name, p)
    return HttpResponse.json(view(p), { status: 201 })
  }),

  http.get(`${BASE}/:name`, ({ params }) => {
    const p = named(params)
    return p ? HttpResponse.json(view(p)) : notFound(String(params.name))
  }),

  http.patch(`${BASE}/:name`, async ({ params, request }) => {
    const p = named(params)
    if (!p) return notFound(String(params.name))
    const body = (await request.json()) as { description?: string; category?: string | null; tags?: string[] }
    if (body.description !== undefined) {
      if (body.description.length > 1024) return problem(422, 'the description is at most 1024 characters', 'validation')
      p.description = body.description
    }
    if (body.category !== undefined) p.category = body.category
    if (body.tags !== undefined) p.tags = body.tags
    p.updated_at = stamp()
    return HttpResponse.json(view(p))
  }),

  http.delete(`${BASE}/:name`, ({ params }) => {
    if (!plugins.delete(String(params.name))) return notFound(String(params.name))
    dropHostedPackage(String(params.name))
    return new HttpResponse(null, { status: 204 })
  }),

  http.get(`${BASE}/:name/files`, ({ params }) => {
    const p = named(params)
    if (!p) return notFound(String(params.name))
    const files = [...p.files].sort(([a], [b]) => (a < b ? -1 : 1)).map(([path, f]) => fileView(path, f))
    return HttpResponse.json({
      files,
      total_bytes: files.reduce((n, f) => n + f.size, 0),
      max_files: MAX_FILES,
      max_file_bytes: MAX_FILE_BYTES,
      max_total_bytes: MAX_TOTAL_BYTES,
    })
  }),

  http.post(`${BASE}/:name/files/moves`, async ({ params, request }) => {
    const p = named(params)
    if (!p) return notFound(String(params.name))
    const { from, to } = (await request.json()) as { from: string; to: string }
    const moved = p.files.get(from)
    if (!moved) return problem(404, `the draft has no file ${from}`, 'file-not-found')
    const refused = pathProblem(to, moved.executable)
    if (refused) return problem(422, `${to}: ${refused}`, 'path-refused')
    if (to === from) return HttpResponse.json(fileView(to, moved))
    if (p.files.has(to)) return problem(409, `the draft already has ${to}`, 'file-exists')
    const clashes = clash(to, [...p.files.keys()].filter((f) => f !== from))
    if (clashes) return problem(409, clashes, 'path-clash')
    p.files.delete(from)
    const next = { ...moved, updated_at: stamp() }
    p.files.set(to, next)
    return HttpResponse.json(fileView(to, next))
  }),

  http.get(`${BASE}/:name/files/*`, ({ params }) => {
    const p = named(params)
    if (!p) return notFound(String(params.name))
    const path = pathOf(params)
    const f = p.files.get(path)
    if (!f) return problem(404, `the draft has no file ${path}`, 'file-not-found')
    return new HttpResponse(f.bytes.slice().buffer, {
      headers: { 'Content-Type': 'application/octet-stream', 'X-ScadBuddy-Executable': String(f.executable) },
    })
  }),

  http.put(`${BASE}/:name/files/*`, async ({ params, request }) => {
    const p = named(params)
    if (!p) return notFound(String(params.name))
    const path = pathOf(params)
    const executable = new URL(request.url).searchParams.get('executable') === 'true'
    const refused = pathProblem(path, executable)
    if (refused) return problem(422, `${path}: ${refused}`, 'path-refused')
    const bytes = new Uint8Array(await request.arrayBuffer())
    if (bytes.length > MAX_FILE_BYTES) return problem(413, `a file is at most ${MAX_FILE_BYTES} bytes`, 'file-too-large')
    const others = [...p.files.keys()].filter((f) => f !== path)
    const clashes = clash(path, others)
    if (clashes) return problem(409, clashes, 'path-clash')
    if (!p.files.has(path) && p.files.size >= MAX_FILES) return problem(413, `a draft holds at most ${MAX_FILES} files`, 'too-many-files')
    const total = others.reduce((n, f) => n + p.files.get(f)!.bytes.length, 0) + bytes.length
    if (total > MAX_TOTAL_BYTES) return problem(413, `a draft holds at most ${MAX_TOTAL_BYTES} bytes`, 'draft-too-large')
    put(p, path, bytes, executable)
    return HttpResponse.json(fileView(path, p.files.get(path)!))
  }),

  http.delete(`${BASE}/:name/files/*`, ({ params }) => {
    const p = named(params)
    if (!p) return notFound(String(params.name))
    const path = pathOf(params)
    if (!p.files.delete(path)) return problem(404, `the draft has no file ${path}`, 'file-not-found')
    p.updated_at = stamp()
    return new HttpResponse(null, { status: 204 })
  }),

  http.get(`${BASE}/:name/draft-diff`, ({ params }) => {
    const p = named(params)
    return p ? HttpResponse.json(diff(p)) : notFound(String(params.name))
  }),

  http.get(`${BASE}/:name/versions`, ({ params }) => {
    const p = named(params)
    if (!p) return notFound(String(params.name))
    return HttpResponse.json([...p.versions].reverse().map(({ description: _description, ...v }) => v))
  }),

  http.post(`${BASE}/:name/publish`, async ({ params }) => {
    await delay(100)
    const p = named(params)
    if (!p) return notFound(String(params.name))
    const found = draftProblems(p)
    if (found.length) return problem(422, 'the draft cannot be published as it is', 'draft-invalid', { problems: found })
    const changes = diff(p)
    const base = p.versions.at(-1)
    if (base && !changes.added.length && !changes.removed.length && !changes.changed.length && !changes.manifest_changed) {
      return problem(409, `the draft is the same as version ${base.version}`, 'nothing-to-publish')
    }
    const { description: _description, ...version } = publish(p)
    return HttpResponse.json(version)
  }),
]
