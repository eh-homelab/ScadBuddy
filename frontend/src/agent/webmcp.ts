import type { AgentBridge } from './bridge'

/**
 * WebMCP: the page's live tools, offered to an agent built into the browser.
 *
 * API shape as the explainer and draft spec have it, read 2026-09-27:
 * https://github.com/webmachinelearning/webmcp (README "Imperative Tool Registration")
 * and the draft spec https://github.com/webmachinelearning/webmcp/blob/main/index.bs:
 *
 *   partial interface Document { [SecureContext, SameObject] readonly attribute ModelContext modelContext; };
 *   Promise<undefined> registerTool(ModelContextTool tool, optional ModelContextRegisterToolOptions options = {});
 *
 * A tool is `{ name, description, inputSchema, execute, annotations? }` and is
 * unregistered by aborting the `signal` passed at registration. The annotations used are
 * `readOnlyHint` and `consequentialHint`. `registerTool` rejects with `NotAllowedError` in
 * a cross-origin iframe without `allow="tools"` (README "Permissions policy and iframes").
 * Bambuddy's iframe, as CLAUDE.md records it, sets `sandbox` and no `allow`, so embedded
 * the registration is expected to be refused; a rejection is ignored.
 *
 * The issue (#254) names `navigator.modelContext`; the current explainer and spec put it
 * on `document`, so that is what is feature-detected. Where it is missing this is a no-op.
 */
interface ModelContextTool {
  name: string
  description: string
  inputSchema: Record<string, unknown>
  execute: (input: unknown) => Promise<unknown>
  annotations?: { readOnlyHint?: boolean; consequentialHint?: boolean }
}

interface ModelContext {
  registerTool: (tool: ModelContextTool, options?: { signal?: AbortSignal }) => Promise<undefined>
}

function modelContext(): ModelContext | undefined {
  const candidate = (document as Document & { modelContext?: Partial<ModelContext> }).modelContext
  return candidate && typeof candidate.registerTool === 'function' ? (candidate as ModelContext) : undefined
}

/**
 * Keeps the browser's tool list in step with the bridge's live tools: every change of
 * route re-registers the set. Returns the teardown.
 */
export function connectWebMcp(bridge: AgentBridge): () => void {
  const context = modelContext()
  if (!context) return () => {}

  let controller: AbortController | null = null
  let generation = 0

  async function sync() {
    const mine = ++generation
    controller?.abort()
    controller = new AbortController()
    const { signal } = controller
    const tools = await bridge.listTools()
    if (mine !== generation) return
    for (const tool of tools) {
      await context!
        .registerTool(
          {
            name: tool.name,
            description: tool.description,
            inputSchema: tool.inputSchema,
            annotations: {
              readOnlyHint: tool.risk === 'read',
              consequentialHint: tool.risk === 'outward',
            },
            // The same path as every other caller: validation, `unavailable`, typed errors.
            execute: async (input) => {
              const outcome = await bridge.call(tool.name, input)
              return { content: [{ type: 'text', text: JSON.stringify(outcome) }] }
            },
          },
          { signal },
        )
        .catch(() => undefined)
    }
  }

  const unsubscribe = bridge.subscribe(() => void sync())
  void sync()
  return () => {
    unsubscribe()
    generation++
    controller?.abort()
  }
}
