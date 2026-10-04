import type { Payload } from '@temporalio/common'
import { randomBytes, randomUUID } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { kekFromBase64, SealError } from '../src/secrets.js'
import {
  isSubject,
  openPayloadBytes,
  SubjectPayloadCodec,
  unwrapDataKey,
} from '../src/temporal/codec.js'
import type { PayloadKeys } from '../src/temporal/payloadKeys.js'

// The per-subject payload codec (spec 2026-10-01 §6.5, plan ruling 11): payloads of
// session-*/flow-* workflows are sealed under that workflow's data key; others pass.

/** In-memory keys: one data key per subject, gone after `forget`. */
class MemoryKeys implements PayloadKeys {
  readonly keys = new Map<string, Buffer>()
  async createKey(subject: string): Promise<void> {
    if (!this.keys.has(subject)) this.keys.set(subject, randomBytes(32))
  }
  async dataKey(subject: string): Promise<Buffer | undefined> {
    return this.keys.get(subject)
  }
  async forget(subject: string): Promise<void> {
    this.keys.delete(subject)
  }
}

const SESSION = `session-${randomUUID()}`
const payload = (text: string): Payload => ({
  metadata: { encoding: Buffer.from('json/plain'), extra: Buffer.from('x') },
  data: Buffer.from(JSON.stringify({ text })),
})
const asBuffers = (p: Payload) => ({
  metadata: Object.fromEntries(Object.entries(p.metadata ?? {}).map(([k, v]) => [k, Buffer.from(v)])),
  data: Buffer.from(p.data ?? new Uint8Array()),
})

describe('isSubject', () => {
  it('names session and flow workflow ids only', () => {
    expect(isSubject(SESSION)).toBe(true)
    expect(isSubject(`flow-${randomUUID()}`)).toBe(true)
    expect(isSubject('render-x')).toBe(false)
    expect(isSubject('print-run-y')).toBe(false)
    expect(isSubject(`session-${randomUUID()}-x`)).toBe(false)
  })
})

describe('SubjectPayloadCodec', () => {
  it.each([
    ['workflow', { type: 'workflow', namespace: 'n', workflowId: SESSION } as const],
    ['activity', { type: 'activity', namespace: 'n', workflowId: SESSION, activityId: 'a', isLocal: false } as const],
  ])('seals a %s payload of a session and opens it again', async (_, context) => {
    const keys = new MemoryKeys()
    await keys.createKey(SESSION)
    const codec = new SubjectPayloadCodec(keys)
    const original = [payload('hello'), payload('again')]
    const encoded = await codec.encode(original, context)
    expect(encoded).toHaveLength(2)
    for (const p of encoded) {
      expect(Buffer.from(p.metadata!['encoding']!).toString()).toBe('binary/encrypted')
      expect(Buffer.from(p.metadata!['encryption-key-id']!).toString()).toBe(SESSION)
      expect(Buffer.from(p.data!).toString()).not.toContain('hello')
    }
    // Decode trusts encryption-key-id, not the context.
    const decoded = await codec.decode(encoded)
    expect(decoded.map(asBuffers)).toEqual(original.map(asBuffers))
  })

  it.each([
    ['render-x', { type: 'workflow', namespace: 'n', workflowId: 'render-x' } as const],
    ['print-run-y', { type: 'activity', namespace: 'n', workflowId: 'print-run-y', isLocal: false } as const],
    ['no context', undefined],
  ])('passes %s through unchanged', async (_, context) => {
    const codec = new SubjectPayloadCodec(new MemoryKeys())
    const original = [payload('plain')]
    expect(await codec.encode(original, context)).toEqual(original)
    expect(await codec.decode(original, context)).toEqual(original)
  })

  it('fails closed when the subject has no key', async () => {
    const codec = new SubjectPayloadCodec(new MemoryKeys())
    await expect(codec.encode([payload('x')], { type: 'workflow', namespace: 'n', workflowId: SESSION })).rejects.toThrow(
      SealError,
    )
  })

  it('refuses to decode once the key is forgotten', async () => {
    const keys = new MemoryKeys()
    await keys.createKey(SESSION)
    const codec = new SubjectPayloadCodec(keys)
    const encoded = await codec.encode([payload('x')], { type: 'workflow', namespace: 'n', workflowId: SESSION })
    await keys.forget(SESSION)
    const failure = codec.decode(encoded)
    await expect(failure).rejects.toThrow(SealError)
    await expect(failure).rejects.toThrow(`payload key for ${SESSION} is gone`)
  })
})

describe('the committed payload vectors', () => {
  type Vector = { subject: string; dek_b64: string; dek_sealed_b64: string; plaintext_b64: string; sealed_b64: string }
  const fixture = JSON.parse(readFileSync(new URL('./fixtures/secret-vectors.json', import.meta.url), 'utf8')) as {
    kek_base64: string
    payload: Vector[]
  }
  const kek = kekFromBase64(fixture.kek_base64)

  it.each(fixture.payload.map((v) => [v.subject, v] as const))('unwraps and opens %s', (_, v) => {
    const dek = unwrapDataKey(kek.key, v.subject, Buffer.from(v.dek_sealed_b64, 'base64'))
    expect(dek.toString('base64')).toBe(v.dek_b64)
    const plaintext = openPayloadBytes(dek, v.subject, Buffer.from(v.sealed_b64, 'base64'))
    expect(plaintext.toString('base64')).toBe(v.plaintext_b64)
  })
})
