import type { Payload, PayloadCodec, SerializationContext } from '@temporalio/common'
// CommonJS: Node's ESM loader finds no named exports in it.
import proto from '@temporalio/proto'
import { openBytes, SealError, sealBytes } from '../secrets.js'
import type { PayloadKeys } from './payloadKeys.js'

// The per-subject payload codec (spec 2026-10-01 §6.5, plan ruling 11). A subject is
// a workflow ID, session-<uuid> or flow-<uuid>. Every payload of a subject's workflow
// is sealed, as a serialized Payload, under that subject's data key with AAD
// `ai_payload:<subject>`, into a payload with `encoding: binary/encrypted` and
// `encryption-key-id: <subject>` (Temporal's encryption sample's names). Payloads of
// any other workflow, or with no context, pass through. Encoding a subject that has
// no key fails closed; decoding one whose key is gone throws. agent-durable's
// scadbuddy_durable/codec.py is the same codec in Python.

const SUBJECT = /^(session|flow)-[0-9a-f-]{36}$/
const ENCRYPTED = 'binary/encrypted'
const { Payload: PayloadProto } = proto.temporal.api.common.v1

export function isSubject(workflowId: string): boolean {
  return SUBJECT.test(workflowId)
}

/** The AAD a subject's data key is sealed with under the KEK. */
export const dataKeyContext = (subject: string): string => `dek:ai_payload_keys:${subject}`
const payloadContext = (subject: string): string => `ai_payload:${subject}`

/** Opens a subject's sealed data key (an `ai_payload_keys.dek_sealed`) with the KEK's key. */
export function unwrapDataKey(kekKey: Buffer, subject: string, dekSealed: Buffer): Buffer {
  return openBytes(kekKey, dekSealed, dataKeyContext(subject))
}

export function wrapDataKey(kekKey: Buffer, subject: string, dek: Buffer): Buffer {
  return sealBytes(kekKey, dek, dataKeyContext(subject))
}

export function openPayloadBytes(dek: Buffer, subject: string, sealed: Buffer): Buffer {
  return openBytes(dek, sealed, payloadContext(subject))
}

function subjectOf(context: SerializationContext | undefined): string | undefined {
  const workflowId = context?.workflowId
  return workflowId !== undefined && isSubject(workflowId) ? workflowId : undefined
}

export class SubjectPayloadCodec implements PayloadCodec {
  readonly #keys: PayloadKeys
  constructor(keys: PayloadKeys) {
    this.#keys = keys
  }

  async encode(payloads: Payload[], context?: SerializationContext): Promise<Payload[]> {
    const subject = subjectOf(context)
    if (subject === undefined) return payloads
    const key = await this.#keys.dataKey(subject)
    if (key === undefined) throw new SealError(`no payload key for ${subject}; refusing to write it unencrypted`)
    return payloads.map((payload) => {
      const bytes = Buffer.from(PayloadProto.encode(payload).finish())
      try {
        return {
          metadata: { encoding: Buffer.from(ENCRYPTED), 'encryption-key-id': Buffer.from(subject) },
          data: sealBytes(key, bytes, payloadContext(subject)),
        }
      } finally {
        bytes.fill(0)
      }
    })
  }

  // Trusts encryption-key-id rather than the context, so a payload a workflow carries
  // from another subject still opens.
  async decode(payloads: Payload[], _context?: SerializationContext): Promise<Payload[]> {
    const out: Payload[] = []
    for (const payload of payloads) {
      const encoding = payload.metadata?.['encoding']
      if (encoding === undefined || encoding === null || Buffer.from(encoding).toString() !== ENCRYPTED) {
        out.push(payload)
        continue
      }
      const keyId = payload.metadata?.['encryption-key-id']
      if (!keyId) throw new SealError('encrypted payload names no encryption-key-id')
      const subject = Buffer.from(keyId).toString()
      const key = await this.#keys.dataKey(subject)
      if (key === undefined) throw new SealError(`payload key for ${subject} is gone`)
      const bytes = openPayloadBytes(key, subject, Buffer.from(payload.data ?? new Uint8Array()))
      try {
        // The decoded fields can share `bytes`' memory, so they are copied before it is zeroed.
        const decoded = PayloadProto.decode(bytes)
        const metadata = Object.fromEntries(Object.entries(decoded.metadata).map(([k, v]) => [k, Buffer.from(v)]))
        out.push({ metadata, data: Buffer.from(decoded.data) })
      } finally {
        bytes.fill(0)
      }
    }
    return out
  }
}
