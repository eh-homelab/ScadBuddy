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

/** Throws, naming the event, when the panel would drop it. */
export async function expectPanelAccepts(events: readonly unknown[]): Promise<void> {
  const parse = await frontendParseServerEvent()
  for (const e of events) {
    const result = parse(e)
    if (!result.ok) throw new Error(`the panel would drop ${JSON.stringify(e)}: ${result.error}`)
  }
}
