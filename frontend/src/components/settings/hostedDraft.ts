/**
 * #6937 — what the hosted plugin editor works out without the server: the draft's path rules as
 * a first check (the API's `hosted_plugins/rules.py` is the authority, and its refusal is shown
 * as it is), the file tree, the editor's language, a new skill's `SKILL.md`, and how far the
 * latest version has reached the assistant (spec 2026-11-21 §6.3 step 5, §6.5).
 */
import type { PluginPackage } from '../../api/aiPlugins'
import { TOP_LEVEL } from '../../api/hostedPlugins'

/** Why `path` cannot be a draft file, or null; the API checks again at write. */
export function pathProblem(path: string, executable = false): string | null {
  if (path.trim() === '') return 'Enter a path, such as skills/my-skill/SKILL.md.'
  if (path.includes('\\')) return 'Separate folders with /, not \\.'
  if (path.startsWith('/')) return "Write the path from the plugin's root, without a leading /."
  const segments = path.split('/')
  if (segments.some((s) => s === '')) return 'The path has an empty folder name (two / in a row, or a / at the end).'
  if (segments.some((s) => s.startsWith('.'))) return 'No file or folder name may start with "." (no hidden files, no "..").'
  if (segments.length < 2 || !(TOP_LEVEL as readonly string[]).includes(segments[0]!)) {
    return 'A file must be under skills/, agents/ or commands/.'
  }
  if (executable && !canBeExecutable(path)) return 'Only a file under skills/ can be executable.'
  return null
}

/** Only a skill may hold a script (spec §6.8). */
export const canBeExecutable = (path: string) => path.startsWith('skills/')

/** A skill's folder name: what the frontmatter's `name` is too. */
export function skillNameProblem(name: string): string | null {
  if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(name)) return 'Use lower-case letters, digits and hyphens, starting with a letter or digit.'
  return null
}

export function newSkill(name: string): string {
  return `---\nname: ${name}\ndescription: Say what this skill does and when the assistant should use it.\n---\n\nWrite the skill's instructions here.\n`
}

export type TreeNode = { kind: 'folder'; name: string; path: string; children: TreeNode[] } | { kind: 'file'; name: string; path: string }

/** The draft's paths as folders and files, folders first, each level sorted. */
export function fileTree(paths: readonly string[]): TreeNode[] {
  const root: TreeNode[] = []
  for (const path of paths) {
    const parts = path.split('/')
    let level = root
    parts.forEach((part, i) => {
      const at = parts.slice(0, i + 1).join('/')
      if (i === parts.length - 1) {
        level.push({ kind: 'file', name: part, path: at })
        return
      }
      let folder = level.find((n): n is Extract<TreeNode, { kind: 'folder' }> => n.kind === 'folder' && n.name === part)
      if (!folder) {
        folder = { kind: 'folder', name: part, path: at, children: [] }
        level.push(folder)
      }
      level = folder.children
    })
  }
  const sort = (nodes: TreeNode[]): TreeNode[] =>
    nodes
      .sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === 'folder' ? -1 : 1))
      .map((n) => (n.kind === 'folder' ? { ...n, children: sort(n.children) } : n))
  return sort(root)
}

/** Monaco's language for a draft file; plain text for any other. */
export function languageOf(path: string): string {
  if (/\.(md|markdown)$/i.test(path)) return 'markdown'
  if (/\.py$/i.test(path)) return 'python'
  if (/\.(sh|bash)$/i.test(path)) return 'shell'
  return 'plaintext'
}

/** The bytes as text, or null when they are not UTF-8 (a binary file is not edited here). */
export function asText(bytes: Uint8Array): string | null {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return null
  }
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`
  return `${(n / (1024 * 1024)).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`
}

/**
 * How far a hosted plugin's latest version has reached the assistant, from its package row
 * (spec §6.3 step 5: install or re-pin is chosen by whether the row exists).
 */
export type OfferState =
  | { kind: 'unpublished' }
  /** The latest version is neither the pin nor the pending re-pin: Offer runs the install or re-pin. */
  | { kind: 'not-offered'; version: number; loaded: number | null }
  /** Installed at the latest version, not yet approved. */
  | { kind: 'awaiting-approval'; version: number }
  /** A re-pin to the latest version waits for approval; `loaded` keeps loading until then. */
  | { kind: 'repin-pending'; version: number; loaded: number | null }
  | { kind: 'offered'; version: number; enabled: boolean }
  /** A package of another source has the name, so an Offer is refused. */
  | { kind: 'name-taken'; source: string }

export function offerState(latest: number | null, pkg: PluginPackage | undefined): OfferState {
  if (latest === null) return { kind: 'unpublished' }
  if (!pkg) return { kind: 'not-offered', version: latest, loaded: null }
  if (pkg.source.kind !== 'hosted') return { kind: 'name-taken', source: pkg.source.kind }
  if (pkg.pending?.version === latest) return { kind: 'repin-pending', version: latest, loaded: pkg.version }
  if (pkg.version === latest) return pkg.approved ? { kind: 'offered', version: latest, enabled: pkg.enabled } : { kind: 'awaiting-approval', version: latest }
  return { kind: 'not-offered', version: latest, loaded: pkg.version }
}

/** What an offer did, from the package row the install answered (re-pin or install, spec §6.3 step 5). */
export function offerResultText(pkg: PluginPackage): string {
  return pkg.pending
    ? `Offered version ${pkg.pending.version} as a re-pin. Version ${pkg.version} loads until you approve it.`
    : `Offered version ${pkg.version}. It is installed and waits for your approval.`
}

const CATEGORY_RE = /^[a-z0-9][a-z0-9-]{0,63}$/
const TAG_RE = /^[a-z0-9][a-z0-9-]{0,31}$/
/** The API's cap on a plugin's tags (`api/hosted_plugins.py` `HostedPluginCreate.tags`). */
export const MAX_TAGS = 20

/** The tags typed in one field, split at commas and blanks, each once, in order. */
export function parseTags(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).filter((t) => t !== ''))]
}

/** Why a category or a tag list cannot be saved, or null; the API checks again (`api/hosted_plugins.py`). */
export function detailsProblem(category: string, tags: readonly string[]): string | null {
  if (category !== '' && !CATEGORY_RE.test(category)) return 'A category is 1 to 64 lower-case letters, digits and hyphens.'
  const bad = tags.find((t) => !TAG_RE.test(t))
  if (bad !== undefined) return `"${bad}" is not a tag. A tag is 1 to 32 lower-case letters, digits and hyphens.`
  if (tags.length > MAX_TAGS) return `A plugin has at most ${MAX_TAGS} tags.`
  return null
}

/**
 * Who published a version (`published_by`, backend `api/hosted_plugins.py` `_by`), as a person
 * reads it: "you" for the signed-in person, the assistant for the agent's credential, a
 * signed-in user by the subject of `user:<issuer>#<sub>`, and the other principals as
 * `principalLabel` (`agent/chat/labels.ts`) names them. An id it does not know is shown as it is.
 */
export function publishedByText(by: string, me: string | null | undefined, label: (principal: string) => string): string {
  if (me && by === me) return 'you'
  if (by === 'service:agent') return 'the assistant'
  if (by === 'anonymous') return 'an anonymous caller'
  if (by.startsWith('user:')) {
    const hash = by.lastIndexOf('#')
    const subject = hash < 0 ? '' : by.slice(hash + 1)
    return subject ? `user ${subject}` : by
  }
  return label(by)
}
