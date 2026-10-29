import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { writeFile } from 'node:fs/promises'
import { createBackendClient } from '../api/backend.js'
import type { Tier } from '../auth/principal.js'
import { DURABLE_ONLY_NAMES, DURABLE_ONLY_TOOLS } from './answerTools.js'
import { ALL_TOOLS } from './index.js'
import { PendingActionStore } from './pending.js'
import { createExternalServer } from './projections.js'
import type { Tool } from './registry.js'

// ALL_TOOLS as the durable worker declares them (spec 2026-10-01 §6.3, #1055): each
// tool's name, description and input schema exactly as /mcp lists them, and its tier,
// and its gate kind (`hitl`, spec §6.6): `approval` for a tool whose call waits for a
// human approval, `answer` for the durable-only tools that ask the user
// (tools/answerTools.ts), null for the rest; DurableSession parks a call with a `hitl`
// at its gate. The list is read through the /mcp projection itself, so the two cannot
// differ; the durable-only tools are projected for the manifest only, never served on
// /mcp. Generated at build (`dist/tools.json`, `pnpm gen:tools`), never committed,
// like the API clients (#492).

export type ToolManifestEntry = {
  name: string
  description: string
  input_schema: Record<string, unknown>
  tier: Tier
  hitl: 'approval' | 'answer' | null
}

/** Every tool a durable session may call: ALL_TOOLS and the durable-only answer tools. */
export const DURABLE_TOOLS: readonly Tool[] = [...ALL_TOOLS, ...DURABLE_ONLY_TOOLS]

function hitlOf(tool: Tool): ToolManifestEntry['hitl'] {
  if (DURABLE_ONLY_NAMES.has(tool.name)) return 'answer'
  return tool.gated ? 'approval' : null
}

const listings = new WeakMap<readonly Tool[], Promise<ReadonlyMap<string, Record<string, unknown>>>>()

async function listOnce(tools: readonly Tool[]): Promise<ReadonlyMap<string, Record<string, unknown>>> {
  // Listing calls no tool, so the services are never used.
  const server = createExternalServer(tools, {
    backend: createBackendClient('http://127.0.0.1:1'),
    pending: new PendingActionStore(),
    pollIntervalMs: 1000,
    renderWaitMs: 0,
  })
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  await server.connect(serverSide)
  const client = new Client({ name: 'scadbuddy-tool-manifest', version: '0' })
  await client.connect(clientSide)
  try {
    const { tools: listed } = await client.listTools()
    return new Map(listed.map((t) => [t.name, t as Record<string, unknown>]))
  } finally {
    await client.close()
  }
}

/**
 * The entries /mcp `tools/list` answers for `tools`, by name: listed through the /mcp
 * projection itself, once per list (and so once per process for the default list).
 */
export function listExternal(tools: readonly Tool[] = DURABLE_TOOLS): Promise<ReadonlyMap<string, Record<string, unknown>>> {
  let listing = listings.get(tools)
  if (listing === undefined) {
    listing = listOnce(tools)
    listings.set(tools, listing)
    // A failed listing is not kept: the next caller lists again.
    listing.catch(() => listings.delete(tools))
  }
  return listing
}

export async function toolManifest(tools: readonly Tool[] = DURABLE_TOOLS): Promise<ToolManifestEntry[]> {
  const listed = await listExternal(tools)
  const byName = new Map(tools.map((t) => [t.name, t]))
  return [...listed.values()]
    .map((t) => ({
      name: t.name as string,
      description: (t.description as string | undefined) ?? '',
      input_schema: t.inputSchema as Record<string, unknown>,
      tier: byName.get(t.name as string)!.risk,
      hitl: hitlOf(byName.get(t.name as string)!),
    }))
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** The manifest as JSON at `file`. */
export async function writeManifest(file: string, tools: readonly Tool[] = DURABLE_TOOLS): Promise<void> {
  await writeFile(file, `${JSON.stringify(await toolManifest(tools), null, 2)}\n`)
}
