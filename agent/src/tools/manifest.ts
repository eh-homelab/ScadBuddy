import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { writeFile } from 'node:fs/promises'
import { createBackendClient } from '../api/backend.js'
import type { Tier } from '../auth/principal.js'
import { UNTRUSTED_CONTENT_POLICY } from '../safety/untrusted.js'
import { ALL_TOOLS } from './index.js'
import { PendingActionStore } from './pending.js'
import { createExternalServer } from './projections.js'
import type { Tool } from './registry.js'

// ALL_TOOLS as the durable worker declares them (spec 2026-10-01 §6.3, #1055): each
// tool's name, description and input schema exactly as /mcp lists them, and its tier,
// from which `needs_approval` follows (`tier == "outward"`). The list is read through
// the /mcp projection itself, so the two cannot differ. Generated at build
// (`dist/tools.json`, `pnpm gen:tools`), never committed, like the API clients (#492).

export type ToolManifestEntry = {
  name: string
  description: string
  input_schema: Record<string, unknown>
  tier: Tier
}

export async function toolManifest(tools: readonly Tool[] = ALL_TOOLS): Promise<ToolManifestEntry[]> {
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
    const risk = new Map(tools.map((t) => [t.name, t.risk]))
    return listed
      .map((t) => ({
        name: t.name,
        description: t.description ?? '',
        input_schema: t.inputSchema as Record<string, unknown>,
        tier: risk.get(t.name)!,
      }))
      .sort((a, b) => a.name.localeCompare(b.name))
  } finally {
    await client.close()
  }
}

/** The manifest as JSON at `file`. */
export async function writeManifest(file: string, tools: readonly Tool[] = ALL_TOOLS): Promise<void> {
  await writeFile(file, `${JSON.stringify(await toolManifest(tools), null, 2)}\n`)
}

// The durable worker's system-prompt append (`dist/durable-prompt.txt`): the policy every
// session turn carries, so a durable session reads tool results the same way.
export async function writeDurablePrompt(path: string): Promise<void> {
  await writeFile(path, UNTRUSTED_CONTENT_POLICY, 'utf8')
}
