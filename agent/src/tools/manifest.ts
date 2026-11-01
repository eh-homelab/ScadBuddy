import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { createBackendClient } from '../api/backend.js'
import { ALL_TOOLS } from './index.js'
import { PendingActionStore } from './pending.js'
import { createExternalServer } from './projections.js'
import type { Tool } from './registry.js'

// The tools as /mcp lists them (name, description and input schema), read through the
// /mcp projection itself, so a caller that shows a tool's details (access/providers.ts)
// shows what /mcp serves.

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
export function listExternal(tools: readonly Tool[] = ALL_TOOLS): Promise<ReadonlyMap<string, Record<string, unknown>>> {
  let listing = listings.get(tools)
  if (listing === undefined) {
    listing = listOnce(tools)
    listings.set(tools, listing)
    // A failed listing is not kept: the next caller lists again.
    listing.catch(() => listings.delete(tools))
  }
  return listing
}
