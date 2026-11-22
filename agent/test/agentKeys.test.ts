import { hkdfSync } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { CrudFetchGrants, InputHashKey, KeyNotReleasedError } from '../src/api/agentKeys.js'
import { inputHash } from '../src/approvals/service.js'

// The two keys the agent derived from its own KEK, through the API's CRUD routes (#5299 step
// 12; backend api/crud_agent_keys.py, whose tests pin the API's side): the approvals' input-hash
// key, released once and kept, and a one-off fetch's grant, minted by the API.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'

type Answer = { status: number; body: unknown } | 'refused'

function fakeApi(answers: Answer[]): { fetch: typeof fetch; seen: { method: string; path: string; body: unknown }[] } {
  const seen: { method: string; path: string; body: unknown }[] = []
  const fetchImpl: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const text = request.method === 'POST' ? await request.text() : ''
    seen.push({ method: request.method, path: new URL(request.url).pathname.slice(PREFIX.length), body: text ? JSON.parse(text) : undefined })
    const answer = answers.shift() ?? answers.at(-1) ?? 'refused'
    if (answer === 'refused') throw new TypeError('fetch failed', { cause: Object.assign(new Error('connect'), { code: 'ECONNREFUSED' }) })
    return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'x-scadbuddy-crud': '1' } })
  }
  return { fetch: fetchImpl, seen }
}

const KEY = Buffer.alloc(32, 7)

describe('the input-hash key', () => {
  it('throws until the API released it, then gives the released bytes', async () => {
    const api = fakeApi(['refused', { status: 503, body: { detail: 'no KEK', reason: 'kek-missing' } }, { status: 200, body: { key: KEY.toString('base64') } }])
    const logs: string[] = []
    const key = new InputHashKey({ baseUrl: BASE, fetch: api.fetch }, { firstMs: 1, maxMs: 2, log: (m) => logs.push(m) })
    expect(() => key.get()).toThrow(KeyNotReleasedError)
    key.start()
    await key.released()
    expect(key.get()).toEqual(KEY)
    expect(api.seen.map((s) => `${s.method} ${s.path}`)).toEqual(Array(3).fill('GET /keys/input-hash'))
    // Each kind of wait once, never the key.
    expect(logs).toHaveLength(2)
    expect(logs.join('\n')).not.toContain(KEY.toString('base64'))
  })

  it('refuses a key that is not 32 bytes and keeps asking', async () => {
    const api = fakeApi([{ status: 200, body: { key: Buffer.alloc(16).toString('base64') } }, { status: 200, body: { key: KEY.toString('base64') } }])
    const key = new InputHashKey({ baseUrl: BASE, fetch: api.fetch }, { firstMs: 1, maxMs: 2, log: () => undefined })
    key.start()
    await key.released()
    expect(key.get()).toEqual(KEY)
    expect(api.seen).toHaveLength(2)
  })

  it('hashes as the KEK-derived key did, so an earlier hash still matches', () => {
    // backend tests/crud/test_crud_agent_keys.py pins the API's derivation to this one.
    const kek = Buffer.alloc(32, 0x31)
    const derived = Buffer.from(hkdfSync('sha256', kek, Buffer.alloc(0), 'scadbuddy:ai_approvals:input_hash:v1', 32))
    expect(derived.toString('base64')).toBe('Sl3Jx+pP/OVUmJWyOmEw4ogfnYnnhW/M0UZfNJrtyEw=')
    expect(inputHash(derived, 'tool', { a: 1 })).toBe(inputHash(Buffer.from('Sl3Jx+pP/OVUmJWyOmEw4ogfnYnnhW/M0UZfNJrtyEw=', 'base64'), 'tool', { a: 1 }))
  })
})

describe("a one-off fetch's grant", () => {
  it('is minted by the API for the host and the URL', async () => {
    const api = fakeApi([{ status: 200, body: { grant: 'g.mac' } }])
    const grants = new CrudFetchGrants({ baseUrl: BASE, fetch: api.fetch })
    await expect(grants.mint('files.example.com', 'https://files.example.com/a.stl')).resolves.toBe('g.mac')
    expect(api.seen).toEqual([{ method: 'POST', path: '/fetch-grants', body: { host: 'files.example.com', url: 'https://files.example.com/a.stl' } }])
  })

  it("throws the API's refusal", async () => {
    const api = fakeApi([{ status: 503, body: { detail: 'The API has no key-encryption key', reason: 'kek-missing' } }])
    const grants = new CrudFetchGrants({ baseUrl: BASE, fetch: api.fetch })
    await expect(grants.mint('a.example', 'https://a.example/x')).rejects.toThrow(/no key-encryption key/)
  })
})
