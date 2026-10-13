import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { ALL_TOOLS } from '../src/tools/index.js'
import { DURABLE_ONLY_TOOLS } from '../src/tools/answerTools.js'
import { DURABLE_TOOLS, toolManifest, writeManifest } from '../src/tools/manifest.js'
import { createExternalServer } from '../src/tools/projections.js'
import { services } from './helpers/mcp.js'

// The manifest the durable worker declares its tools from (spec 2026-10-01 §6.3,
// #1055): what /mcp lists, plus each tool's tier.

async function mcpListing() {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  await createExternalServer(ALL_TOOLS, services()).connect(serverSide)
  const client = new Client({ name: 'manifest-test', version: '0' })
  await client.connect(clientSide)
  const { tools } = await client.listTools()
  await client.close()
  return tools
}

describe('the tool manifest', () => {
  it('is what /mcp lists, with every tool, its tier and its gate kind, plus the durable-only answer tools', async () => {
    const manifest = await toolManifest()
    expect(manifest.map((t) => t.name)).toEqual(DURABLE_TOOLS.map((t) => t.name).sort())
    const byName = new Map(DURABLE_TOOLS.map((t) => [t.name, t]))
    for (const entry of manifest) {
      expect(entry.tier).toBe(byName.get(entry.name)!.risk)
      expect(entry.hitl, entry.name).toBe(byName.get(entry.name)!.gated ? 'approval' : entry.name.match(/^(ask_user|wait_for_user)$/) ? 'answer' : null)
    }
    expect(manifest.filter((t) => t.hitl === 'answer').map((t) => t.name)).toEqual(['ask_user', 'wait_for_user'])
    const listed = new Map((await mcpListing()).map((t) => [t.name, t]))
    // Durable-only: never on /mcp.
    for (const tool of DURABLE_ONLY_TOOLS) expect(listed.has(tool.name), tool.name).toBe(false)
    for (const entry of manifest.filter((e) => listed.has(e.name))) {
      const tool = listed.get(entry.name)!
      expect({ name: entry.name, description: entry.description, input_schema: entry.input_schema }).toEqual({
        name: tool.name,
        description: tool.description,
        input_schema: tool.inputSchema,
      })
    }
  })

  it('is written as JSON', async () => {
    const dir = await mkdtemp(path.join(tmpdir(), 'manifest-'))
    try {
      const out = path.join(dir, 'tools.json')
      await writeManifest(out)
      const written = JSON.parse(await readFile(out, 'utf8')) as { name: string }[]
      expect(written.map((t) => t.name)).toEqual((await toolManifest()).map((t) => t.name))
    } finally {
      await rm(dir, { recursive: true, force: true })
    }
  })
})
