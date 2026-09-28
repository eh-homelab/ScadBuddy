/**
 * msw stand-ins for the agent service's plugin routes (#297): remote MCP endpoints
 * (`/api/v1/ai/plugins`, agent/src/routes/plugins.ts) and plugin packages
 * (`/api/v1/ai/plugin-packages`, agent/src/routes/pluginPackages.ts). The rules the
 * UI depends on are kept: an install is stored unapproved and disabled; approving
 * needs the exact commit and hash; only an approved pin can be enabled; a re-pin is
 * pending with a diff until approved; a refused package answers 422 with `problems`.
 *
 * Git URLs the mock knows: `https://git.example/greeter.git` (clean; `ref: "v2"`
 * gives a second commit), `https://git.example/shell.git` (refused), and the
 * marketplace `https://git.example/market.git` with entry `greeter`.
 */
import { HttpResponse, delay, http } from 'msw'
import type {
  PackageReview,
  PluginPackage,
  PluginTest,
  RemotePlugin,
  RemotePluginCreate,
  RemotePluginPatch,
} from '../api/aiPlugins'

const base = '/api/v1/ai'

export const GREETER_V1 = {
  commit: '3f1c9a2b7d4e5f60718293a4b5c6d7e8f9012345',
  hash: 'sha256:9b2e6c1f0a3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b',
}
export const GREETER_V2 = {
  commit: '7a8b9c0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f607',
  hash: 'sha256:1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d',
}
export const SHELL_PROBLEMS = [
  'skills/status/SKILL.md: runs a shell command through dynamic context injection (!`...`)',
  'hooks/hooks.json: Stop has a "command" hook',
]

const REVIEW_V1: PackageReview = {
  name: 'greeter',
  description: 'Says hello.',
  version: '1.0.0',
  skills: ['greeter:hello'],
  commands: ['greeter:wave'],
  agents: ['greeter:helper'],
  hooks: [{ event: 'Stop', type: 'prompt' }],
  mcp_servers: [{ name: 'mem', type: 'http', url: 'https://mcp.example/mcp/' }],
  files: ['.claude-plugin/plugin.json', '.mcp.json', 'README.md', 'agents/helper.md', 'commands/wave.md', 'hooks/hooks.json', 'skills/hello/SKILL.md'],
}
const REVIEW_V2: PackageReview = {
  ...REVIEW_V1,
  version: '2.0.0',
  skills: ['greeter:bye', 'greeter:hello'],
  files: [...REVIEW_V1.files.filter((f) => f !== 'README.md'), 'skills/bye/SKILL.md'].sort(),
}

interface State {
  remote: Map<string, RemotePlugin>
  packages: Map<string, PluginPackage>
}

const state: State = { remote: new Map(), packages: new Map() }

const now = () => new Date('2026-09-28T09:00:00Z').toISOString()

export function resetAiPluginMocks(): void {
  state.remote.clear()
  state.packages.clear()
  state.remote.set('hindsight', {
    name: 'hindsight',
    kind: 'remote_mcp',
    url: 'https://hindsight.example/mcp/bank-1/',
    enabled: false,
    tool_prefix: 'mcp__hindsight__',
    auth: { header: 'Authorization', last4: '9f3a' },
    usable: true,
    tool_tiers: { recall: 'read' },
    disabled_tools: [],
    created_at: now(),
    updated_at: now(),
  })
}
resetAiPluginMocks()

const detail = (status: number, text: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json({ detail: text, ...extra }, { status })

function packageFor(url: string, ref: string, kind: 'git' | 'marketplace', entry?: string): PluginPackage | string[] | undefined {
  const known = url === 'https://git.example/greeter.git' || (kind === 'marketplace' && url === 'https://git.example/market.git' && entry === 'greeter')
  if (url === 'https://git.example/shell.git') return SHELL_PROBLEMS
  if (!known) return undefined
  const v = ref === 'v2' ? GREETER_V2 : GREETER_V1
  return {
    name: 'greeter',
    source:
      kind === 'git'
        ? { kind: 'git', url, ref, path: '' }
        : { kind: 'marketplace', url, ref, entry: entry ?? '', plugin_url: url, plugin_path: 'plugins/greeter' },
    commit_sha: v.commit,
    content_hash: v.hash,
    review: ref === 'v2' ? REVIEW_V2 : REVIEW_V1,
    approved: false,
    approved_at: null,
    enabled: false,
    pending: null,
    created_at: now(),
    updated_at: now(),
  }
}

function toolsFor(plugin: RemotePlugin): PluginTest['tools'] {
  return ['recall', 'retain', 'files.list'].map((tool) => {
    const renamed = tool.includes('.')
    const harness = tool.replace(/[^A-Za-z0-9_-]/g, '_')
    const explicit = !renamed && Object.hasOwn(plugin.tool_tiers, tool)
    return {
      name: tool,
      harness_name: `${plugin.tool_prefix}${harness}`,
      description: `The ${tool} tool.`,
      annotations: tool === 'recall' ? { readOnlyHint: true } : null,
      tier: explicit ? plugin.tool_tiers[tool]! : 'outward',
      tier_source: renamed ? 'renamed' : explicit ? 'explicit' : 'default',
      collides_with: [],
      suggested_tier: tool === 'recall' ? 'read' : null,
      disabled: plugin.disabled_tools.includes(tool),
    }
  })
}

export const aiPluginHandlers = [
  // ---- remote MCP endpoints
  http.get(`${base}/plugins`, () => HttpResponse.json([...state.remote.values()])),

  http.post(`${base}/plugins`, async ({ request }) => {
    const body = (await request.json()) as RemotePluginCreate
    if (!/^[a-z][a-z0-9-]{0,30}[a-z0-9]$/.test(body.name)) {
      return detail(400, 'name must be 2–32 characters: lower-case letters, digits and single hyphens')
    }
    if (!/^https:\/\//.test(body.url)) return detail(400, 'url must be https: plain http is allowed only to loopback')
    if (state.remote.has(body.name)) return detail(409, `a plugin named "${body.name}" already exists`)
    const plugin: RemotePlugin = {
      name: body.name,
      kind: 'remote_mcp',
      url: body.url,
      enabled: false,
      tool_prefix: `mcp__${body.name}__`,
      auth: body.secret ? { header: body.auth_header || 'Authorization', last4: body.secret.slice(-4) } : null,
      usable: true,
      tool_tiers: {},
      disabled_tools: [],
      created_at: now(),
      updated_at: now(),
    }
    state.remote.set(plugin.name, plugin)
    return HttpResponse.json(plugin, { status: 201 })
  }),

  http.patch(`${base}/plugins/:name`, async ({ params, request }) => {
    const plugin = state.remote.get(String(params.name))
    if (!plugin) return detail(404, `no plugin named "${String(params.name)}"`)
    const body = (await request.json()) as RemotePluginPatch
    const next: RemotePlugin = {
      ...plugin,
      ...(body.enabled === undefined ? {} : { enabled: body.enabled }),
      ...(body.tool_tiers ? { tool_tiers: body.tool_tiers } : {}),
      ...(body.disabled_tools ? { disabled_tools: [...body.disabled_tools].sort() } : {}),
      updated_at: now(),
    }
    state.remote.set(plugin.name, next)
    return HttpResponse.json(next)
  }),

  http.delete(`${base}/plugins/:name`, ({ params }) => {
    if (!state.remote.delete(String(params.name))) return detail(404, 'no such plugin')
    return new HttpResponse(null, { status: 204 })
  }),

  http.post(`${base}/plugins/:name/test`, async ({ params }) => {
    await delay(100)
    const plugin = state.remote.get(String(params.name))
    if (!plugin) return detail(404, 'no such plugin')
    const result: PluginTest = {
      ok: true,
      detail: 'connected; 3 tools',
      duration_ms: 42,
      server: { name: 'hindsight', version: '0.4.1' },
      tools: toolsFor(plugin),
      truncated: false,
    }
    return HttpResponse.json(result)
  }),

  // ---- plugin packages
  http.get(`${base}/plugin-packages`, () => HttpResponse.json([...state.packages.values()])),

  http.post(`${base}/plugin-packages`, async ({ request }) => {
    await delay(150)
    const { source } = (await request.json()) as {
      source: { kind: 'git' | 'marketplace'; url: string; ref?: string; path?: string; entry?: string }
    }
    if (!/^https?:\/\//.test(source.url)) return detail(400, 'source url must be https (or http to loopback)')
    const found = packageFor(source.url, source.ref || 'HEAD', source.kind, source.entry)
    if (found === undefined) return detail(502, `git fetch failed: repository ${source.url} not found`)
    if (Array.isArray(found)) return detail(422, 'the plugin package is refused', { problems: found })
    if (state.packages.has(found.name)) {
      return detail(409, `a plugin package named "${found.name}" is already installed; re-pin it instead`)
    }
    state.packages.set(found.name, found)
    return HttpResponse.json(found, { status: 201 })
  }),

  http.post(`${base}/plugin-packages/:name/approve`, async ({ params, request }) => {
    const pkg = state.packages.get(String(params.name))
    if (!pkg) return detail(404, 'no such plugin package')
    const body = (await request.json()) as { commit_sha: string; content_hash: string }
    if (pkg.pending && body.commit_sha === pkg.pending.commit_sha && body.content_hash === pkg.pending.content_hash) {
      const next: PluginPackage = {
        ...pkg,
        source: { ...pkg.source, ref: pkg.pending.ref },
        commit_sha: pkg.pending.commit_sha,
        content_hash: pkg.pending.content_hash,
        review: pkg.pending.review,
        approved: true,
        approved_at: now(),
        pending: null,
      }
      state.packages.set(pkg.name, next)
      return HttpResponse.json(next)
    }
    if (body.commit_sha === pkg.commit_sha && body.content_hash === pkg.content_hash) {
      const next = { ...pkg, approved: true, approved_at: pkg.approved_at ?? now() }
      state.packages.set(pkg.name, next)
      return HttpResponse.json(next)
    }
    return detail(409, 'the commit and content hash do not match the pin under review')
  }),

  http.patch(`${base}/plugin-packages/:name`, async ({ params, request }) => {
    const pkg = state.packages.get(String(params.name))
    if (!pkg) return detail(404, 'no such plugin package')
    const { enabled } = (await request.json()) as { enabled: boolean }
    if (enabled && !pkg.approved) return detail(409, 'approve the pin before enabling the package')
    const next = { ...pkg, enabled }
    state.packages.set(pkg.name, next)
    return HttpResponse.json(next)
  }),

  http.post(`${base}/plugin-packages/:name/repin`, async ({ params, request }) => {
    await delay(150)
    const pkg = state.packages.get(String(params.name))
    if (!pkg) return detail(404, 'no such plugin package')
    const { ref } = (await request.json()) as { ref?: string }
    const found = packageFor(pkg.source.url, ref || pkg.source.ref, pkg.source.kind, pkg.source.kind === 'marketplace' ? pkg.source.entry : undefined)
    if (!found || Array.isArray(found)) return detail(502, 'git fetch failed')
    if (found.commit_sha === pkg.commit_sha) return detail(409, `"${pkg.name}" is already pinned to ${pkg.commit_sha}`)
    const removed = pkg.review.files.filter((f) => !found.review.files.includes(f))
    const added = found.review.files.filter((f) => !pkg.review.files.includes(f))
    const next: PluginPackage = {
      ...pkg,
      pending: {
        ref: found.source.ref,
        commit_sha: found.commit_sha,
        content_hash: found.content_hash,
        review: found.review,
        diff: { added, removed, changed: ['.claude-plugin/plugin.json', 'skills/hello/SKILL.md'] },
      },
    }
    state.packages.set(pkg.name, next)
    return HttpResponse.json(next)
  }),

  http.delete(`${base}/plugin-packages/:name/pending`, ({ params }) => {
    const pkg = state.packages.get(String(params.name))
    if (!pkg) return detail(404, 'no such plugin package')
    const next = { ...pkg, pending: null }
    state.packages.set(pkg.name, next)
    return HttpResponse.json(next)
  }),

  http.delete(`${base}/plugin-packages/:name`, ({ params }) => {
    if (!state.packages.delete(String(params.name))) return detail(404, 'no such plugin package')
    return new HttpResponse(null, { status: 204 })
  }),
]
