import type { Payload } from '@temporalio/common'
import { readFileSync, writeFileSync } from 'node:fs'
import { inspect } from 'node:util'
import { describe, expect, it } from 'vitest'
import {
  OFFLOAD_BYTES,
  openPayload,
  type PayloadStore,
  PgPayloadKeys,
  referenceOf,
  referencePayload,
  payloadKeyContext,
  type PayloadKeys,
  sealPayload,
  SUBJECT_ENCODING,
  SubjectForgotten,
  SubjectPayloadCodec,
  subjectOf,
} from '../src/temporal/payloadCodec.js'
import { kekFromBase64, openBytes, type RandomSource, sealBytes } from '../src/secrets.js'

// The payload codec of durable subjects (spec 2026-10-01 §6.5, plan 5c Ruling 11).
// The vectors are the one source of truth for its format: this test seals fixed
// payloads with fixed bytes and fails if the committed file differs; agent-durable's
// tests/test_codec.py opens every one and seals them again to the same bytes.
// Regenerate: UPDATE_PAYLOAD_VECTORS=1 pnpm vitest run test/payloadCodec.test.ts
const FILE = new URL('./fixtures/payload-vectors.json', import.meta.url)

function counter(seed: number): RandomSource {
  let n = seed
  return (size) => {
    const out = Buffer.alloc(size)
    for (let i = 0; i < size; i++) out[i] = (n++ * 131 + 7) & 0xff
    return out
  }
}

const KEK_B64 = Buffer.alloc(32, 0x5a).toString('base64')
const kek = kekFromBase64(KEK_B64)
const SUBJECT = 'session-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
const enc = (s: string) => new TextEncoder().encode(s)
const b64 = (b: Uint8Array | null | undefined) => Buffer.from(b ?? new Uint8Array()).toString('base64')
const view = (p: Payload) => ({
  metadata: Object.fromEntries(Object.entries(p.metadata ?? {}).map(([k, v]) => [k, b64(v)])),
  data: b64(p.data),
})

const PLAIN: { name: string; payload: Payload }[] = [
  { name: 'json', payload: { metadata: { encoding: enc('json/plain') }, data: enc('{"text":"Make it red, é ✓"}') } },
  { name: 'null', payload: { metadata: { encoding: enc('binary/null') }, data: new Uint8Array() } },
  {
    name: 'two metadata keys',
    payload: { metadata: { messageType: enc('temporal.api.Thing'), encoding: enc('json/protobuf') }, data: enc('{}') },
  },
]

function build() {
  const random = counter(4242)
  const dek = random(32)
  const dekSealed = sealBytes(kek.key, dek, payloadKeyContext(SUBJECT), random)
  const payloads = PLAIN.map((v, i) => ({
    name: v.name,
    plain: view(v.payload),
    iv_b64: counter(100 * (i + 1))(12).toString('base64'),
    encoded: view(sealPayload(dek, SUBJECT, v.payload, counter(100 * (i + 1)))),
  }))
  return { kek_b64: KEK_B64, kek_id: kek.id, subject: SUBJECT, dek_sealed_b64: dekSealed.toString('base64'), payloads }
}

class FixedKeys implements PayloadKeys {
  readonly asked: [string, boolean][] = []
  readonly keys: Map<string, Buffer>
  constructor(keys: Map<string, Buffer>) {
    this.keys = keys
  }
  async keyFor(subject: string, create: boolean): Promise<Buffer> {
    this.asked.push([subject, create])
    const key = this.keys.get(subject)
    if (key) return key
    if (!create) throw new SubjectForgotten(subject)
    const made = Buffer.alloc(32, this.keys.size + 1)
    this.keys.set(subject, made)
    return made
  }
}

describe('payload vectors', () => {
  const built = build()

  it('match the committed file', () => {
    if (process.env.UPDATE_PAYLOAD_VECTORS === '1') writeFileSync(FILE, `${JSON.stringify(built, null, 2)}\n`)
    expect(JSON.parse(readFileSync(FILE, 'utf8'))).toEqual(built)
  })

  it('open to the original payloads', () => {
    const dek = openBytes(kek.key, Buffer.from(built.dek_sealed_b64, 'base64'), payloadKeyContext(SUBJECT))
    built.payloads.forEach((v, i) => {
      const encoded: Payload = {
        metadata: Object.fromEntries(Object.entries(v.encoded.metadata).map(([k, x]) => [k, new Uint8Array(Buffer.from(x, 'base64'))])),
        data: new Uint8Array(Buffer.from(v.encoded.data, 'base64')),
      }
      expect(view(openPayload(dek, SUBJECT, encoded))).toEqual(view(PLAIN[i]!.payload))
    })
  })
})

describe('SubjectPayloadCodec', () => {
  const plain: Payload = { metadata: { encoding: enc('json/plain') }, data: enc('"the user\'s words"') }

  it("seals a session's and a flow's payloads, and passes every other workflow's through", async () => {
    const keys = new FixedKeys(new Map())
    const codec = new SubjectPayloadCodec(keys)
    for (const workflowId of [SUBJECT, 'flow-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f61']) {
      const [sealed] = await codec.encode([plain], { type: 'workflow', namespace: 'default', workflowId })
      expect(new TextDecoder().decode(sealed!.metadata!.encoding!)).toBe(SUBJECT_ENCODING)
      expect(Buffer.from(sealed!.data!).toString()).not.toContain('the user')
      // Decoded without any context: the subject is in the payload.
      expect(view((await codec.decode([sealed!]))[0]!)).toEqual(view(plain))
    }
    for (const workflowId of ['render-abc', 'session-not-a-uuid', 'AgentOperation-x', `${SUBJECT}-x`]) {
      expect(await codec.encode([plain], { type: 'workflow', namespace: 'default', workflowId })).toEqual([plain])
    }
    expect(await codec.encode([plain])).toEqual([plain])
    expect(await codec.encode([plain], { type: 'activity', namespace: 'default', isLocal: false })).toEqual([plain])
    const [fromActivity] = await codec.encode([plain], { type: 'activity', namespace: 'default', workflowId: SUBJECT, isLocal: false })
    expect(fromActivity!.data).not.toEqual(plain.data)
    // A payload not ours decodes as it is.
    expect(await codec.decode([plain])).toEqual([plain])
  })

  it("fails to decode a forgotten subject's payload, and never makes it a key", async () => {
    const keys = new FixedKeys(new Map())
    const codec = new SubjectPayloadCodec(keys)
    const [sealed] = await codec.encode([plain], { type: 'workflow', namespace: 'default', workflowId: SUBJECT })
    keys.keys.clear()
    await expect(codec.decode([sealed!])).rejects.toBeInstanceOf(SubjectForgotten)
    expect(keys.asked.at(-1)).toEqual([SUBJECT, false])
  })

  it('refuses a payload altered, or moved to another subject', async () => {
    const keys = new FixedKeys(new Map())
    const codec = new SubjectPayloadCodec(keys)
    const [sealed] = await codec.encode([plain], { type: 'workflow', namespace: 'default', workflowId: SUBJECT })
    const other = 'session-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f62'
    keys.keys.set(other, keys.keys.get(SUBJECT)!)
    const moved = { ...sealed!, metadata: { ...sealed!.metadata, 'scadbuddy-subject': enc(other) } }
    await expect(codec.decode([moved])).rejects.toThrow(/authentication/)
    const data = Buffer.from(sealed!.data!)
    data[data.length - 1]! ^= 1
    await expect(codec.decode([{ ...sealed!, data: new Uint8Array(data) }])).rejects.toThrow(/authentication/)
  })

  // Security review of 5c (sensitive-data-exposure, main.ts): what main.ts can log of the
  // codec (an error's message, the keys object itself) never carries a key or payload bytes.
  it('never puts a key or a payload in an error or an inspected object', async () => {
    const keys = new FixedKeys(new Map())
    const codec = new SubjectPayloadCodec(keys)
    const [sealed] = await codec.encode([plain], { type: 'workflow', namespace: 'default', workflowId: SUBJECT })
    const key = keys.keys.get(SUBJECT)!
    const data = Buffer.from(sealed!.data!)
    data[data.length - 1]! ^= 1
    const err = (await codec.decode([{ ...sealed!, data: new Uint8Array(data) }]).catch((e: unknown) => e)) as Error
    const shown = `${err.message} ${String(err.stack)}`
    for (const secret of [key.toString('base64'), key.toString('hex'), 'the user', b64(sealed!.data).slice(0, 16)]) {
      expect(shown).not.toContain(secret)
    }
    const pg = new PgPayloadKeys({} as never, kek)
    const inspected = inspect(pg, { showHidden: true, depth: 5 })
    expect(inspected).not.toContain(kek.key.toString('hex').slice(0, 16))
    expect(inspected).not.toContain(KEK_B64.slice(0, 16))
    expect(JSON.stringify(pg)).not.toContain(KEK_B64.slice(0, 16))
  })

  // Security review of 5c (crypto-key-lifecycle): forgetting or evicting a cached key
  // zeroes the cache's copy, never the key an encode in flight was handed.
  it('hands out a copy of a cached key, so evicting it never zeroes a key in use', async () => {
    const dek = Buffer.alloc(32, 7)
    const other = 'session-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f63'
    const rows = new Map([SUBJECT, other].map((s) => [s, sealBytes(kek.key, dek, payloadKeyContext(s))]))
    const sql = (async (_strings: TemplateStringsArray, subject: string) => [{ dek_sealed: rows.get(subject), kek_id: kek.id }]) as never
    const keys = new PgPayloadKeys(sql, kek, { cacheMax: 1 })
    const first = await keys.keyFor(SUBJECT, false)
    const again = await keys.keyFor(SUBJECT, false)
    expect(again).not.toBe(first)
    await keys.keyFor(other, false) // evicts SUBJECT's entry
    keys.forget(other)
    expect(first.equals(dek)).toBe(true)
    expect(again.equals(dek)).toBe(true)
  })

  it('names the subject form it seals', () => {
    expect(subjectOf(SUBJECT)).toBe(SUBJECT)
    expect(subjectOf(SUBJECT.toUpperCase())).toBeUndefined()
    expect(subjectOf(undefined)).toBeUndefined()
  })
})

// #2243: a session's large payloads are kept in ai_payload_blobs, history holding a
// Temporal ExternalStorageReference that agent-durable's External Storage also reads.
class MemoryStore implements PayloadStore {
  readonly rows = new Map<string, Uint8Array>()
  async put(subject: string, digest: string, data: Uint8Array): Promise<void> {
    if (!this.rows.has(`${subject}/${digest}`)) this.rows.set(`${subject}/${digest}`, data)
  }
  async get(subject: string, digest: string): Promise<Uint8Array | undefined> {
    return this.rows.get(`${subject}/${digest}`)
  }
}

describe('SubjectPayloadCodec with a store', () => {
  const big: Payload = { metadata: { encoding: enc('json/plain') }, data: enc(`"${'z'.repeat(OFFLOAD_BYTES)}"`) }
  const small: Payload = { metadata: { encoding: enc('json/plain') }, data: enc('"hi"') }
  const ctx = (workflowId: string) => ({ type: 'workflow' as const, namespace: 'default', workflowId })

  it("stores a session's large payload by reference, and reads it back", async () => {
    const store = new MemoryStore()
    const codec = new SubjectPayloadCodec(new FixedKeys(new Map()), store)
    const [ref, inline] = await codec.encode([big, small], ctx(SUBJECT))
    expect(referenceOf(ref!)).toEqual({ subject: SUBJECT, digest: expect.stringMatching(/^[0-9a-f]{64}$/) })
    expect((ref!.data ?? new Uint8Array()).length).toBeLessThan(256)
    expect(Buffer.from(ref!.data!).toString()).not.toContain('zzzz')
    expect(new TextDecoder().decode(inline!.metadata!.encoding!)).toBe(SUBJECT_ENCODING)
    expect(store.rows.size).toBe(1)
    expect((await codec.decode([ref!, inline!])).map(view)).toEqual([view(big), view(small)])
  })

  it("keeps a flow's payloads inline, and refuses a reference it cannot read", async () => {
    const store = new MemoryStore()
    const keys = new FixedKeys(new Map())
    const codec = new SubjectPayloadCodec(keys, store)
    const [sealed] = await codec.encode([big], ctx('flow-0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f61'))
    expect(new TextDecoder().decode(sealed!.metadata!.encoding!)).toBe(SUBJECT_ENCODING)
    expect(store.rows.size).toBe(0)
    const [ref] = await codec.encode([big], ctx(SUBJECT))
    await expect(new SubjectPayloadCodec(keys).decode([ref!])).rejects.toThrow(/no store is set/)
    store.rows.clear()
    await expect(codec.decode([ref!])).rejects.toBeInstanceOf(SubjectForgotten)
  })

  it('reads the reference agent-durable writes', () => {
    // agent-durable tests/test_payload_store.py builds this same payload with Temporal's own converter.
    const digest = 'ab'.repeat(32)
    const python: Payload = {
      metadata: { messageType: enc('temporal.api.sdk.v1.ExternalStorageReference'), encoding: enc('json/protobuf') },
      data: enc(`{"claimData":{"digest":"${digest}","subject":"${SUBJECT}"},"driverName":"scadbuddy-pg"}`),
    }
    expect(referenceOf(python)).toEqual({ subject: SUBJECT, digest })
    expect(view(referencePayload(SUBJECT, digest))).toEqual(view(python))
  })
})
