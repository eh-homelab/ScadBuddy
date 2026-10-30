import { vi } from 'vitest'
import type { PackagePin, PackageRepo, PackageView } from '../../src/plugins/packages/store.js'
import type { PackageReview } from '../../src/plugins/packages/vet.js'
import { PluginError } from '../../src/plugins/registry.js'

/** A hook id in `packageReview`'s one hook. */
export const HOOK = '0123456789abcdef'

export function packageReview(over: Partial<PackageReview> = {}): PackageReview {
  return {
    name: 'notes',
    description: null,
    version: null,
    skills: ['notes:jot'],
    commands: ['notes:jot', 'notes:wave'],
    agents: ['notes:review'],
    hooks: [{ event: 'Stop', type: 'http', url: 'https://hooks.example/stop', id: HOOK }],
    mcp_servers: [{ name: 'api', type: 'http', url: 'https://api.example/mcp' }],
    local_servers: [],
    files: [],
    refused: [],
    builtin_tools: [],
    described: { 'skill:jot': 'Jots a note', 'skill:wave': null, 'subagent:review': 'Reviews' },
    aliases: {},
    cut: [],
    ...over,
  }
}

export function packageView(name: string, over: Partial<PackageView> = {}, rev: Partial<PackageReview> = {}): PackageView {
  return {
    name,
    source: { kind: 'git', url: `https://git.example/${name}.git`, ref: 'main', path: '' },
    commit_sha: 'c'.repeat(40),
    content_hash: `sha256:${name}`,
    review: packageReview({ name, ...rev }),
    approved: true,
    approved_at: '2026-10-29T00:00:00.000Z',
    allow_refused: false,
    enabled: true,
    pending: null,
    created_at: '2026-10-29T00:00:00.000Z',
    updated_at: '2026-10-29T00:00:00.000Z',
    ...over,
  }
}

export function memoryPackages(initial: PackageView[]) {
  const views = new Map(initial.map((v) => [v.name, v]))
  const reject = () => Promise.reject(new Error('not called'))
  const setEnabled = vi.fn(async (name: string, enabled: boolean) => {
    const found = views.get(name)
    if (!found) throw new PluginError(`no plugin package named "${name}"`, 404)
    if (enabled && !found.approved) throw new PluginError('approve the pin before enabling the package (issue #297, "Review before enable")', 409)
    const next = { ...found, enabled }
    views.set(name, next)
    return next
  })
  const pinOf = async (name: string) => {
    const v = views.get(name)
    if (!v) return undefined
    const pin: PackagePin & { source: { kind: 'git'; url: string; ref: string; path: string } } = {
      name,
      fetchUrl: `https://git.example/${name}.git`,
      fetchPath: '',
      commit: v.commit_sha,
      contentHash: v.content_hash,
      allowRefused: v.allow_refused,
      source: { kind: 'git', url: `https://git.example/${name}.git`, ref: 'main', path: '' },
    }
    return pin
  }
  const repo: PackageRepo = {
    list: async () => [...views.values()].sort((a, b) => (a.name < b.name ? -1 : 1)),
    get: async (name) => views.get(name),
    pinOf,
    create: reject,
    setPending: reject,
    discardPending: reject,
    approve: reject,
    setEnabled,
    delete: reject,
    enabledPins: reject,
    filesOf: reject,
  }
  return { repo, views, setEnabled }
}
