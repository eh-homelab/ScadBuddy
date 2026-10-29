import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { BUILTIN_SOURCES } from '../../access/model.js'
import { sourceEnabled } from '../../access/resolve.js'
import type { AccessReader, AccessWriter } from '../../access/store.js'
import type { AuditContext } from '../../audit/log.js'
import { OWN_PLUGIN_DIR } from '../../harness/ownPlugin.js'
import type { PackageFileEntry } from './files.js'
import { OWN_PLUGIN_NAME, type PackageReview, vetPackage } from './vet.js'

// The plugins that ship in the agent image, listed beside the installed plugin
// packages so Settings shows and manages them like any other: each has a review
// and can be enabled or disabled, but never removed or re-pinned, and a package
// of the same name is never installed (install.ts BuiltInPluginError).
//
//   - `scadbuddy`: ScadBuddy's own plugin (harness/ownPlugin.ts, #896), its
//     skills and subagents. On unless its source `builtin:own-plugin` is
//     switched off in `ai_access` (#2171).

/** The `ai_settings` key its switch was kept under until #2171; the access migration copied it. */
export const SETTING_OWN_PLUGIN = 'scadbuddy_plugin_enabled'

type BuiltIn = {
  name: string
  dir: string
  /** Where it lives in the repository, for the source line. */
  path: string
  /** Its source in `ai_access`, whose `*` row is its switch (on by default, as every `builtin:` source). */
  source: string
  /** Shown when an install names it. */
  installRefusal: string
  version?: string
}

export const BUILT_INS: readonly BuiltIn[] = [
  {
    name: OWN_PLUGIN_NAME,
    dir: OWN_PLUGIN_DIR,
    path: 'agent/plugins/scadbuddy',
    source: BUILTIN_SOURCES.ownPlugin,
    installRefusal:
      "ScadBuddy's own plugin is built in: the assistant already loads its skills and subagents, with its tools in-process. There is nothing to install.",
  },
]

export function builtInNamed(name: string): BuiltIn | undefined {
  return BUILT_INS.find((b) => b.name === name)
}

/** A built-in plugin as routes show it, beside the installed packages (store.ts PackageView). */
export type BuiltInPackageView = {
  name: string
  built_in: true
  source: { kind: 'built_in'; path: string }
  review: PackageReview
  /** Always: it ships with the image, so there is no pin to approve. */
  approved: true
  enabled: boolean
}

const reviews = new Map<string, PackageReview>()

/** Its files' review, once per process; the vetting rules' refusals do not apply to it. */
export function reviewOf(b: BuiltIn): PackageReview {
  let review = reviews.get(b.name)
  if (!review) {
    const vetting = vetPackage(b.dir, b.name)
    if (vetting.fatal.length) {
      console.warn(`built-in plugin ${b.name}: its review is empty: ${vetting.fatal.join('; ')}`)
    }
    const vetted = vetting.fatal.length ? undefined : vetting.review
    review = {
      ...(vetted ?? { skills: [], commands: [], agents: [], hooks: [], mcp_servers: [], files: [], description: null }),
      name: b.name,
      version: b.version ?? vetted?.version ?? null,
      refused: [],
    }
    reviews.set(b.name, review)
  }
  return review
}

const fileLists = new Map<string, PackageFileEntry[]>()

/**
 * Every file of the built-in `name` with its size, for review (#1029), or
 * undefined for any other name. Links are followed: in the repository
 * agent/plugins/scadbuddy links `skills` and `agents` to plugins/scadbuddy
 * (ownPlugin.ts), and the image holds copies. Its files ship with the image, so
 * the list is taken once per process.
 */
export function builtInFiles(name: string): PackageFileEntry[] | undefined {
  const b = builtInNamed(name)
  if (!b) return undefined
  let files = fileLists.get(name)
  if (!files) {
    const out: PackageFileEntry[] = []
    const walk = (rel: string) => {
      for (const entry of readdirSync(path.join(b.dir, rel)).sort()) {
        const p = rel ? `${rel}/${entry}` : entry
        const stat = statSync(path.join(b.dir, p))
        if (stat.isDirectory()) walk(p)
        else if (stat.isFile()) out.push({ path: p, size: stat.size })
      }
    }
    walk('')
    files = out.sort((x, y) => (x.path < y.path ? -1 : x.path > y.path ? 1 : 0))
    fileLists.set(name, files)
  }
  return files
}

/** One file `builtInFiles` lists, or undefined for any other path. */
export function readBuiltInFile(name: string, rel: string): Buffer | undefined {
  const b = builtInNamed(name)
  if (!b || !builtInFiles(name)!.some((f) => f.path === rel)) return undefined
  return readFileSync(path.join(b.dir, rel))
}

export async function builtInEnabled(access: AccessReader | undefined, b: BuiltIn): Promise<boolean> {
  return sourceEnabled(b.source, access ? await access.rows([b.source]) : [])
}

/** Whether ScadBuddy's own plugin loads (sessions/turnRunner.ts reads the same through AccessResolver). */
export function ownPluginEnabled(access: AccessReader | undefined): Promise<boolean> {
  return builtInEnabled(access, builtInNamed(OWN_PLUGIN_NAME)!)
}

async function view(access: AccessReader, b: BuiltIn): Promise<BuiltInPackageView> {
  return {
    name: b.name,
    built_in: true,
    source: { kind: 'built_in', path: b.path },
    review: reviewOf(b),
    approved: true,
    enabled: await builtInEnabled(access, b),
  }
}

export class BuiltInPackages {
  private readonly access: AccessReader & AccessWriter
  constructor(access: AccessReader & AccessWriter) {
    this.access = access
  }

  list(): Promise<BuiltInPackageView[]> {
    return Promise.all(BUILT_INS.map((b) => view(this.access, b)))
  }

  async get(name: string): Promise<BuiltInPackageView | undefined> {
    const b = builtInNamed(name)
    return b ? view(this.access, b) : undefined
  }

  async setEnabled(name: string, enabled: boolean, context: AuditContext): Promise<BuiltInPackageView | undefined> {
    const b = builtInNamed(name)
    if (!b) return undefined
    await this.access.setEnabled(b.source, enabled, context)
    return view(this.access, b)
  }
}
