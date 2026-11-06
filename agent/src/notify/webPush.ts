import { createCipheriv, createECDH, createPrivateKey, hkdfSync, randomBytes, sign, type KeyObject } from 'node:crypto'

// Web Push (#815 §3: a closed tab still alerts): the message encryption of RFC 8291 with
// the `aes128gcm` content coding of RFC 8188, and the VAPID authorization of RFC 8292,
// on node:crypto alone. test/webPush.test.ts checks the encryption against RFC 8291's
// worked example (Appendix A).

/** A P-256 key pair: the 65-octet uncompressed public point and the 32-octet private scalar. */
export type EcKeys = { publicKey: Buffer; privateKey: Buffer }

/** A fresh P-256 key pair (the VAPID key, or a message's ephemeral key). */
export function generateEcKeys(): EcKeys {
  const ecdh = createECDH('prime256v1')
  ecdh.generateKeys()
  return { publicKey: ecdh.getPublicKey(), privateKey: ecdh.getPrivateKey() }
}

/** RFC 8188 §2: one record, so the record size only has to exceed it. */
const RECORD_SIZE = 4096
/** The largest plaintext that fits one record: the size less the 16-octet tag and the delimiter. */
export const PAYLOAD_MAX = RECORD_SIZE - 17

export type Subscriber = {
  /** The browser's `p256dh` key: its P-256 public point, uncompressed. */
  p256dh: Buffer
  /** The browser's 16-octet `auth` secret. */
  auth: Buffer
}

/**
 * The `aes128gcm` body for `plaintext` to `to` (RFC 8291 §3.4, RFC 8188 §2.1): the header
 * (salt, record size, the sender's ephemeral public key) and one record. `salt` and `sender`
 * are for the RFC's example only; left out, each message gets fresh ones.
 */
export function encryptPayload(plaintext: Buffer, to: Subscriber, fixed: { salt?: Buffer; sender?: EcKeys } = {}): Buffer {
  if (plaintext.length > PAYLOAD_MAX) throw new RangeError(`a push payload is at most ${PAYLOAD_MAX} octets`)
  const salt = fixed.salt ?? randomBytes(16)
  const ecdh = createECDH('prime256v1')
  if (fixed.sender) ecdh.setPrivateKey(fixed.sender.privateKey)
  else ecdh.generateKeys()
  const asPublic = ecdh.getPublicKey()
  const shared = ecdh.computeSecret(to.p256dh)
  // RFC 8291 §3.3-3.4: IKM = HKDF(auth, ecdh, "WebPush: info" 0x00 ua_public as_public, 32).
  const keyInfo = Buffer.concat([Buffer.from('WebPush: info\0'), to.p256dh, asPublic])
  const ikm = Buffer.from(hkdfSync('sha256', shared, to.auth, keyInfo, 32))
  // RFC 8188 §2.2-2.3: the content key and the nonce, from the salt.
  const cek = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: aes128gcm\0'), 16))
  const nonce = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: nonce\0'), 12))
  const cipher = createCipheriv('aes-128-gcm', cek, nonce)
  // 0x02: the last (and only) record's delimiter, with no padding.
  const record = Buffer.concat([cipher.update(Buffer.concat([plaintext, Buffer.from([2])])), cipher.final(), cipher.getAuthTag()])
  const header = Buffer.alloc(21)
  salt.copy(header, 0)
  header.writeUInt32BE(RECORD_SIZE, 16)
  header.writeUInt8(asPublic.length, 20)
  return Buffer.concat([header, asPublic, record])
}

/** The private key as a KeyObject, from the raw scalar and public point (node:crypto signs with a KeyObject). */
function signingKey(keys: EcKeys): KeyObject {
  return createPrivateKey({
    format: 'jwk',
    key: {
      kty: 'EC',
      crv: 'P-256',
      d: keys.privateKey.toString('base64url'),
      x: keys.publicKey.subarray(1, 33).toString('base64url'),
      y: keys.publicKey.subarray(33, 65).toString('base64url'),
    },
  })
}

/** How long a VAPID token is good for: RFC 8292 §2 allows at most 24 hours. */
export const VAPID_TTL_S = 12 * 60 * 60

/**
 * The `Authorization` header for a push to `endpoint` (RFC 8292 §2-3): a JWT (ES256) whose
 * audience is the push service's origin and whose `sub` says who to contact, and the key.
 */
export function vapidAuthorization(endpoint: string, keys: EcKeys, subject: string, now = Date.now()): string {
  const b64 = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url')
  const unsigned = `${b64({ typ: 'JWT', alg: 'ES256' })}.${b64({
    aud: new URL(endpoint).origin,
    exp: Math.floor(now / 1000) + VAPID_TTL_S,
    sub: subject,
  })}`
  const signature = sign('sha256', Buffer.from(unsigned), { key: signingKey(keys), dsaEncoding: 'ieee-p1363' })
  return `vapid t=${unsigned}.${signature.toString('base64url')}, k=${keys.publicKey.toString('base64url')}`
}
