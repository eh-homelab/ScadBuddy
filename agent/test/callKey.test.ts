import { createHash } from 'node:crypto'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { TOOL_USE_META } from '../src/telemetry/trace.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { callIds, createHarnessServer } from '../src/tools/projections.js'
import { idempotencyKey } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'

// One model call, one Idempotency-Key (unified runtime spec 2026-10-13 §7): the key a call's
// command sends is derived from the call (ToolContext.callKey), not drawn fresh, so a second run
// of the same call (a released turn's approved call run again by a retried activity) is answered
// from the backend's operation record instead of doing it twice.

const DEFINITION = '6f1c2b0e-1d2a-4c3b-9e8f-0a1b2c3d4e5f'
const sha = (s: string) => createHash('sha256').update(s).digest('hex')

describe('idempotencyKey', () => {
  it("is the call key's digest, which the backend's follow URL can carry", () => {
    const key = idempotencyKey({ callKey: 'durable:s1:run-1:toolu_1' })
    expect(key).toBe(sha('durable:s1:run-1:toolu_1'))
    expect(key).toMatch(/^[0-9a-f]{64}$/)
    expect(idempotencyKey({ callKey: 'durable:s1:run-1:toolu_1' })).toBe(key)
    expect(idempotencyKey({ callKey: 'durable:s1:run-1:toolu_2' })).not.toBe(key)
  })

  it('is fresh for a call with no key', () => {
    const a = idempotencyKey({})
    expect(a).toMatch(/^[0-9a-f]{32}$/)
    expect(idempotencyKey({})).not.toBe(a)
  })

  it("keys a session's call by its tool_use id, and only a session's", () => {
    expect(callIds('s1', 'toolu_1')).toEqual({ toolUseId: 'toolu_1', callKey: 'call:s1:toolu_1' })
    expect(callIds(undefined, 'toolu_1')).toEqual({ toolUseId: 'toolu_1' })
    expect(callIds('s1', undefined)).toEqual({ toolUseId: undefined })
  })
})

describe("the harness server's calls", () => {
  async function keysOf(session: string | undefined, toolUseIds: (string | undefined)[]): Promise<(string | null)[]> {
    const keys: (string | null)[] = []
    const backend = createBackendClient(BACKEND, async (request) => {
      keys.push((request as Request).headers.get('Idempotency-Key'))
      return Response.json({ id: 'r1', status: 'starting', repeated: false }, { status: 202 })
    })
    const principal = { id: 'browser', kind: 'browser' as const, tiers: tiersUpTo('outward') }
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await createHarnessServer(ALL_TOOLS, services({ backend }), principal, session).instance.connect(serverSide)
    const client = new Client({ name: 'call-key-test', version: '0' })
    await client.connect(clientSide)
    try {
      for (const id of toolUseIds) {
        const result = await client.callTool({
          name: 'start_flow_run',
          arguments: { definition_id: DEFINITION, approval_timeout: 'never' },
          ...(id ? { _meta: { [TOOL_USE_META]: id } } : {}),
        })
        expect(result.isError, JSON.stringify(result)).toBeFalsy()
      }
    } finally {
      await client.close()
    }
    return keys
  }

  it('send one key for one model call, however often it runs, and another for the next call', async () => {
    expect(await keysOf('s1', ['toolu_1', 'toolu_1', 'toolu_2'])).toEqual([sha('call:s1:toolu_1'), sha('call:s1:toolu_1'), sha('call:s1:toolu_2')])
  })

  it('send a fresh key for each call outside a session, or without a tool_use id', async () => {
    const keys = [...(await keysOf(undefined, ['toolu_1', 'toolu_1'])), ...(await keysOf('s1', [undefined, undefined]))]
    expect(keys.every((k) => /^[0-9a-f]{32}$/.test(k ?? ''))).toBe(true)
    expect(new Set(keys).size).toBe(4)
  })
})
