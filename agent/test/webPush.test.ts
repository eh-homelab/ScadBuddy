import { createPublicKey, verify } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { encryptPayload, generateEcKeys, PAYLOAD_MAX, vapidAuthorization } from '../src/notify/webPush.js'

// Web Push's encryption and VAPID (#815 §3), on node:crypto.

const b = (text: string) => Buffer.from(text.replaceAll(/\s/g, ''), 'base64url')

describe('encryptPayload', () => {
  it("produces RFC 8291's worked example (Section 5, Appendix A) from its inputs", () => {
    const body = encryptPayload(Buffer.from('When I grow up, I want to be a watermelon'), {
      p256dh: b('BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4'),
      auth: b('BTBZMqHH6r4Tts7J_aSIgg'),
    }, {
      salt: b('DGv6ra1nlYgDCS1FRnbzlw'),
      sender: {
        publicKey: b('BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8'),
        privateKey: b('yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw'),
      },
    })
    const header = b(`DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z 9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml
      mlMoZIIgDll6e3vCYLocInmYWAmS6Tlz AC8wEqKK6PBru3jl7A8`)
    const ciphertext = b(`8pfeW0KbunFT06SuDKoJH9Ql87S1QUrd irN6GcG7sFz1y1sqLgVi1VhjVkHsUoEs bI_0LpXMuGvnzQ`)
    expect(body.toString('base64url')).toBe(Buffer.concat([header, ciphertext]).toString('base64url'))
  })

  it('refuses a payload over one record', () => {
    const to = { p256dh: generateEcKeys().publicKey, auth: Buffer.alloc(16, 1) }
    expect(() => encryptPayload(Buffer.alloc(PAYLOAD_MAX + 1), to)).toThrow(RangeError)
    expect(encryptPayload(Buffer.alloc(PAYLOAD_MAX), to).length).toBe(86 + PAYLOAD_MAX + 17)
  })
})

describe('vapidAuthorization', () => {
  it("signs a JWT for the push service's origin with the key it names (RFC 8292)", () => {
    const keys = generateEcKeys()
    const header = vapidAuthorization('https://push.example.net/send/abc?x=1', keys, 'https://scadbuddy.example', 1_700_000_000_000)
    const match = /^vapid t=([^.]+)\.([^.]+)\.([^,]+), k=(.+)$/.exec(header)
    expect(match).not.toBeNull()
    const [, h, p, s, k] = match!
    expect(JSON.parse(Buffer.from(h!, 'base64url').toString())).toEqual({ typ: 'JWT', alg: 'ES256' })
    expect(JSON.parse(Buffer.from(p!, 'base64url').toString())).toEqual({
      aud: 'https://push.example.net',
      exp: 1_700_000_000 + 12 * 3600,
      sub: 'https://scadbuddy.example',
    })
    expect(Buffer.from(k!, 'base64url').equals(keys.publicKey)).toBe(true)
    const publicKey = createPublicKey({
      format: 'jwk',
      key: { kty: 'EC', crv: 'P-256', x: keys.publicKey.subarray(1, 33).toString('base64url'), y: keys.publicKey.subarray(33).toString('base64url') },
    })
    expect(verify('sha256', Buffer.from(`${h}.${p}`), { key: publicKey, dsaEncoding: 'ieee-p1363' }, Buffer.from(s!, 'base64url'))).toBe(true)
  })
})
