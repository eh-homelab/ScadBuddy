import { takeSnapshot, type Snapshot } from './snapshot'
import {
  AgentToolError,
  type CallResult,
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

/** zod and the schemas load on first use, so the entry chunk carries neither. */
const loadCatalog = () => import('./catalog')

/**
 * The tab's side of the browser tools (#254). Pages and components register the
 * handlers valid while they are mounted; `call` validates the arguments against the
 * tool's schema and runs the newest mounted handler, answering a typed error — never a
 * throw — when there is none. The transport to the agent service (#266) and WebMCP
 * (`webmcp.ts`) are both just callers of this object.
 */
export class AgentBridge {
  private registrations: Registration[] = []
  private readonly listeners = new Set<() => void>()
  private nextId = 1
  private route: string | null = null

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
  async listTools({ all = false }: { all?: boolean } = {}): Promise<ToolInfo[]> {
    const { TOOLS } = await loadCatalog()
    const { toJSONSchema } = await import('zod')
    const live = new Set(this.liveNames())
    return (Object.keys(TOOLS) as ToolName[])
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
  }

  async call(name: string, args: unknown = {}): Promise<CallResult> {
    const { TOOLS, SCOPE_ROUTES } = await loadCatalog()
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
