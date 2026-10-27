import type * as CatalogModule from './catalog'
import { takeSnapshot, type Snapshot } from './snapshot'
import {
  AgentToolError,
  type CallResult,
  type ToolError,
  type Scope,
  type ToolImpls,
  type ToolInfo,
  type ToolName,
} from './types'

type AnyImpl = (args: unknown) => unknown

interface Registration {
  id: number
  impls: Partial<Record<ToolName, AnyImpl>>
  /** What `snapshot()` reports under `page` for whoever registered. */
  describe?: () => unknown
  label: string
}

export interface RegisterOptions {
  /** The key this registration's `describe()` appears under in `snapshot().page`. */
  label: string
  describe?: () => unknown
}

type Catalog = typeof CatalogModule

/** zod and the schemas load on first use, so the entry chunk carries neither. */
export type CatalogLoader = () => Promise<Catalog | undefined>

const importCatalog: CatalogLoader = () => import('./catalog')

/** What `listTools()` answers: the tools, or the typed error `call()` would give. */
export type ListToolsResult = { ok: true; tools: ToolInfo[] } | { ok: false; error: ToolError }

/**
 * The lazy tool catalogue, or the `stale_tab` error (#4884). On a tab from an older
 * build the chunk is gone from the server: `installStaleChunkReload` `preventDefault()`s
 * Vite's `vite:preloadError` and reloads, and Vite's preload helper then resolves the
 * import with `undefined` rather than rejecting. Without the reload (the same build
 * failed already, or there is no sessionStorage) the import rejects instead, with a
 * failed fetch of a module. Either way the agent is told the tab is stale, never handed
 * a TypeError. The reload starts a new tab id (`tabId.ts`), so an agent that paired by
 * code has to pair again. Any other rejection (the catalogue throwing as it evaluates)
 * is not staleness, and reloading would not help: that answers `failed`.
 */
/**
 * A module the browser could not fetch, as each engine words it: Chromium's "Failed to
 * fetch dynamically imported module", Firefox's "error loading dynamically imported
 * module", Safari's "Importing a module script failed.", or Vite's preload helper's
 * "Unable to preload CSS for …".
 */
const MODULE_FETCH_FAILURE =
  /dynamically imported module|Importing a module script failed|Unable to preload CSS/i

function isModuleFetchFailure(cause: unknown): boolean {
  return cause instanceof Error && MODULE_FETCH_FAILURE.test(cause.message)
}

async function loadOrStale<T>(load: () => Promise<T | undefined>): Promise<{ module: T } | { error: ToolError }> {
  let module: T | undefined
  try {
    module = await load()
  } catch (cause) {
    const reason = cause instanceof Error ? cause.message : String(cause)
    if (!isModuleFetchFailure(cause)) {
      return { error: { code: 'failed', message: `The tab's tool catalogue failed to load: ${reason}` } }
    }
    return {
      error: {
        code: 'stale_tab',
        message:
          `The tab could not load its tool catalogue (${reason}); it is probably running an older ` +
          'ScadBuddy build. Ask the user to reload the tab; an agent that paired by code must then ' +
          'pair again with browser_pair.',
      },
    }
  }
  if (module) return { module }
  return {
    error: {
      code: 'stale_tab',
      message:
        'The tab was running an older ScadBuddy build and is reloading to the current one; nothing ' +
        'was run. Wait for it to come back, then call again. The reload ends this tab, so an agent ' +
        'that paired by code must pair again with browser_pair.',
    },
  }
}

/**
 * The tab's side of the browser tools (#254). Pages and components register the
 * handlers valid while they are mounted; `call` validates the arguments against the
 * tool's schema and runs the newest mounted handler, answering a typed error — never a
 * throw — when there is none. The socket to the agent service (`link.ts`) and WebMCP
 * (`webmcp.ts`) are both just callers of this object.
 */
export class AgentBridge {
  private registrations: Registration[] = []
  private readonly listeners = new Set<() => void>()
  private nextId = 1
  private route: string | null = null

  private readonly loadCatalog: CatalogLoader

  /** `loadCatalog` is for tests, which stand in for a stale tab's failed import. */
  constructor(loadCatalog: CatalogLoader = importCatalog) {
    this.loadCatalog = loadCatalog
  }

  /** The router's location; the shell reports it so a MemoryRouter or basename is honoured. */
  setRoute(route: string) {
    if (route === this.route) return
    this.route = route
    this.emit()
  }

  currentRoute(): string {
    return this.route ?? `${window.location.pathname}${window.location.search}`
  }

  register(impls: ToolImpls, options: RegisterOptions): () => void {
    const registration: Registration = {
      id: this.nextId++,
      impls: impls as Partial<Record<ToolName, AnyImpl>>,
      describe: options.describe,
      label: options.label,
    }
    this.registrations = [...this.registrations, registration]
    this.emit()
    return () => {
      const before = this.registrations.length
      this.registrations = this.registrations.filter((entry) => entry.id !== registration.id)
      if (this.registrations.length !== before) this.emit()
    }
  }

  /** Called whenever the set of live tools changes (a page mounts or unmounts). */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /** The names of the tools some mounted page provides, in registration order. */
  liveNames(): ToolName[] {
    const names = new Set<ToolName>()
    for (const entry of this.registrations) {
      for (const name of Object.keys(entry.impls) as ToolName[]) names.add(name)
    }
    return [...names]
  }

  /**
   * The live tools for the current route, or with `all` every tool the app has — each
   * marked `live` or not, which is the stable list a session's tool set is built from.
   */
  async listTools({ all = false }: { all?: boolean } = {}): Promise<ListToolsResult> {
    const loaded = await loadOrStale(this.loadCatalog)
    if ('error' in loaded) return { ok: false, error: loaded.error }
    const { TOOLS } = loaded.module
    // Unguarded: the catalogue imports zod statically, so once it has loaded so has zod.
    // Keep the destructure at the import: rolldown tree-shakes a dynamic import by the
    // members read there, and handing the namespace on keeps all of zod (+290 KB).
    const { toJSONSchema } = await import('zod')
    const live = new Set(this.liveNames())
    const tools = (Object.keys(TOOLS) as ToolName[])
      .filter((name) => all || live.has(name))
      .map((name) => {
        const spec = TOOLS[name]
        return {
          name,
          description: spec.description,
          risk: spec.risk,
          scope: spec.scope as Scope,
          // `input`: a field with a default is optional to the caller.
          inputSchema: toJSONSchema(spec.input, { io: 'input' }) as Record<string, unknown>,
          live: live.has(name),
        }
      })
    return { ok: true, tools }
  }

  async call(name: string, args: unknown = {}): Promise<CallResult> {
    const loaded = await loadOrStale(this.loadCatalog)
    if ('error' in loaded) return { ok: false, error: loaded.error }
    const { TOOLS, SCOPE_ROUTES } = loaded.module
    if (!Object.hasOwn(TOOLS, name)) {
      return { ok: false, error: { code: 'unknown_tool', message: `There is no tool called "${name}".` } }
    }
    const tool = name as ToolName
    const spec = TOOLS[tool]
    const impl = this.implFor(tool)
    if (!impl) {
      return {
        ok: false,
        error: {
          code: 'unavailable',
          message:
            `"${tool}" is only available on ${SCOPE_ROUTES[spec.scope]}; the tab is on ` +
            `${this.currentRoute()}. Use navigate first.`,
        },
      }
    }

    const parsed = spec.input.safeParse(args ?? {})
    if (!parsed.success) {
      return {
        ok: false,
        error: {
          code: 'invalid_args',
          message: `The arguments for "${tool}" do not match its schema.`,
          issues: parsed.error.issues.map((issue) => ({
            path: issue.path.map(String).join('.'),
            message: issue.message,
          })),
        },
      }
    }

    try {
      return { ok: true, result: (await impl(parsed.data)) ?? null }
    } catch (cause) {
      if (cause instanceof AgentToolError) {
        return { ok: false, error: { code: cause.code, message: cause.message } }
      }
      return {
        ok: false,
        error: { code: 'failed', message: cause instanceof Error ? cause.message : String(cause) },
      }
    }
  }

  snapshot(): Snapshot {
    const page: Record<string, unknown> = {}
    for (const entry of this.registrations) {
      if (!entry.describe) continue
      try {
        page[entry.label] = entry.describe()
      } catch (cause) {
        page[entry.label] = { error: cause instanceof Error ? cause.message : String(cause) }
      }
    }
    return takeSnapshot({ route: this.currentRoute(), page, tools: this.liveNames() })
  }

  /** The newest mounted handler wins: a dialog over a page can take a name over. */
  private implFor(name: ToolName): AnyImpl | undefined {
    for (let index = this.registrations.length - 1; index >= 0; index--) {
      const impl = this.registrations[index]?.impls[name]
      if (impl) return impl
    }
    return undefined
  }

  private emit() {
    for (const listener of this.listeners) listener()
  }
}

/** The one bridge the app registers with. */
export const bridge = new AgentBridge()
