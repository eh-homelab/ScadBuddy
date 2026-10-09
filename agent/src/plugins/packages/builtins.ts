import type { AuditContext } from '../../audit/log.js'
import {
  PLAYWRIGHT_MCP_VERSION,
  PLUGIN_NAME as BROWSER_PLUGIN_NAME,
  SETTING_HEADLESS_BROWSER,
  VENDORED_PLUGIN_DIR,
} from '../../harness/headlessBrowser.js'
import { OWN_PLUGIN_DIR } from '../../harness/ownPlugin.js'
import { OWN_PLUGIN_NAME, type PackageReview, vetPackage } from './vet.js'

// The plugins that ship in the agent image, listed beside the installed plugin
// packages so Settings shows and manages them like any other: each has a review
// and can be enabled or disabled, but never removed or re-pinned, and a package
// of the same name is never installed (install.ts BuiltInPluginError).
//
//   - `scadbuddy`: ScadBuddy's own plugin (harness/ownPlugin.ts, #896), its
//     skills and subagents. On unless `scadbuddy_plugin_enabled` is stored false.
//   - `playwright`: the vendored headless browser (harness/headlessBrowser.ts,
//     #349). Its switch IS the `headless_browser_enabled` setting, off unless
//     stored true, so this list and Settings → AI headless browser agree.

export const SETTING_OWN_PLUGIN = 'scadbuddy_plugin_enabled'

type BuiltIn = {
  name: string
  dir: string
  /** Where it lives in the repository, for the source line. */
  path: string
  setting: string
  /** Whether it is on when its setting was never stored. */
  enabledByDefault: boolean
  /** Shown when an install names it. */
  installRefusal: string
  version?: string
}

export const BUILT_INS: readonly BuiltIn[] = [
  {
    name: OWN_PLUGIN_NAME,
    dir: OWN_PLUGIN_DIR,
    path: 'agent/plugins/scadbuddy',
    setting: SETTING_OWN_PLUGIN,
    enabledByDefault: true,
    installRefusal:
      "ScadBuddy's own plugin is built in: the assistant already loads its skills and subagents, with its tools in-process. There is nothing to install.",
  },
  {
    name: BROWSER_PLUGIN_NAME,
    dir: VENDORED_PLUGIN_DIR,
    path: 'agent/plugins/playwright',
    setting: SETTING_HEADLESS_BROWSER,
    enabledByDefault: false,
    installRefusal:
      'The Playwright plugin is built in as the headless browser, pinned to @playwright/mcp ' +
      `${PLAYWRIGHT_MCP_VERSION}. Enable it in the plugin list instead of installing it.`,
    version: PLAYWRIGHT_MCP_VERSION,
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

type Settings = {
  get<T>(key: string): Promise<T | undefined>
  set(key: string, value: unknown, context: AuditContext): Promise<void>
}

const reviews = new Map<string, PackageReview>()

/** Its files' review, once per process; the vetting rules' refusals do not apply to it. */
function reviewOf(b: BuiltIn): PackageReview {
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

export async function builtInEnabled(settings: Pick<Settings, 'get'> | undefined, b: BuiltIn): Promise<boolean> {
  const stored = await settings?.get<unknown>(b.setting)
  return b.enabledByDefault ? stored !== false : stored === true
}

/** Whether ScadBuddy's own plugin loads (sessions/manager.ts). */
export function ownPluginEnabled(settings: Pick<Settings, 'get'> | undefined): Promise<boolean> {
  return builtInEnabled(settings, builtInNamed(OWN_PLUGIN_NAME)!)
}

async function view(settings: Settings, b: BuiltIn): Promise<BuiltInPackageView> {
  return {
    name: b.name,
    built_in: true,
    source: { kind: 'built_in', path: b.path },
    review: reviewOf(b),
    approved: true,
    enabled: await builtInEnabled(settings, b),
  }
}

export class BuiltInPackages {
  private readonly settings: Settings
  constructor(settings: Settings) {
    this.settings = settings
  }

  list(): Promise<BuiltInPackageView[]> {
    return Promise.all(BUILT_INS.map((b) => view(this.settings, b)))
  }

  async get(name: string): Promise<BuiltInPackageView | undefined> {
    const b = builtInNamed(name)
    return b ? view(this.settings, b) : undefined
  }

  async setEnabled(name: string, enabled: boolean, context: AuditContext): Promise<BuiltInPackageView | undefined> {
    const b = builtInNamed(name)
    if (!b) return undefined
    await this.settings.set(b.setting, enabled, context)
    return view(this.settings, b)
  }
}
