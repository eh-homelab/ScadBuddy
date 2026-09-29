import path from 'node:path'
import { fileURLToPath } from 'node:url'

// The panel's protocol module (frontend/src/agent/chat/protocol.ts, #340),
// loaded as it is, so the agent's events are checked against the schema the
// panel actually parses with. The path is computed so tsc does not pull the
// frontend package into the agent's program; vitest.config.ts points its zod
// import at agent's copy.

type ParseResult = { ok: true; value: unknown } | { ok: false; error: string }

const here = path.dirname(fileURLToPath(import.meta.url))
const modulePath = path.resolve(here, '../../../frontend/src/agent/chat/protocol.ts')

export async function frontendParseServerEvent(): Promise<(raw: unknown) => ParseResult> {
  const mod = (await import(modulePath)) as { parseServerEvent: (raw: unknown) => ParseResult }
  return mod.parseServerEvent
}

/** The panel's own client-message builder and parser, to feed what it sends into the agent. */
export async function frontendClientMessages(): Promise<{
  clientMessage: (body: Record<string, unknown> & { type: string }) => Record<string, unknown>
  parseClientMessage: (raw: unknown) => ParseResult
}> {
  return (await import(modulePath)) as Awaited<ReturnType<typeof frontendClientMessages>>
}

export type PanelChatState = {
  sessions: Record<string, { status: string; owner: { kind: string }; items: Record<string, unknown>[] }>
  activeId: string | null
  awaitingStart: boolean
  notice: string | null
}

/**
 * The panel's own stream reducer (frontend/src/agent/chat/state.ts), to replay
 * what a socket delivered and assert on what the panel would show.
 */
export async function frontendChatReducer(): Promise<{
  chatReducer: (state: PanelChatState, action: Record<string, unknown>) => PanelChatState
  initialChatState: PanelChatState
}> {
  const statePath = path.resolve(here, '../../../frontend/src/agent/chat/state.ts')
  return (await import(statePath)) as Awaited<ReturnType<typeof frontendChatReducer>>
}

/** Throws, naming the event, when the panel would drop it. */
export async function expectPanelAccepts(events: readonly unknown[]): Promise<void> {
  const parse = await frontendParseServerEvent()
  for (const e of events) {
    const result = parse(e)
    if (!result.ok) throw new Error(`the panel would drop ${JSON.stringify(e)}: ${result.error}`)
  }
}
