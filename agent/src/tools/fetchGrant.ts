import { createHash, createHmac, hkdfSync, randomBytes } from 'node:crypto'
import type { Kek } from '../secrets.js'

// The one-off host grant (#959): what tells the backend that a fetch off the asset
// allowlist was approved by a person. fetch_asset mints it only as it runs, which is
// only after its call's approval (`allow_host` pins it outward, pinnedOutward.ts), and
// sends it in FETCH_GRANT_HEADER. The backend (`library/fetch_grants.py`) checks the MAC,
// that it names the host it will fetch from and the very URL, that it has not expired,
// and spends its nonce, so a grant serves one fetch.
//
// The format, fixed on both sides:
// `b64url("asset-fetch-once\n<host>\n<sha256 hex of the URL>\n<expiry seconds>\n<nonce hex>")
//  + "." + b64url(HMAC-SHA256)`, under HKDF-SHA256(KEK, no salt, FETCH_GRANT_KEY_INFO).
// A grant is a bearer secret: never log one.

export const FETCH_GRANT_HEADER = 'X-ScadBuddy-Fetch-Grant'
export const FETCH_GRANT_KEY_INFO = 'scadbuddy:assets:fetch-once:v1'
/** How long a grant is good for: the fetch is sent as soon as it is minted. */
export const FETCH_GRANT_TTL_SECONDS = 600

export type FetchGrantMinter = (host: string, url: string) => string

export function fetchGrantKey(kek: Kek): Buffer {
  return Buffer.from(hkdfSync('sha256', kek.key, Buffer.alloc(0), FETCH_GRANT_KEY_INFO, 32))
}

export function fetchGrantMinter(
  key: Buffer,
  now: () => number = Date.now,
  nonce: () => string = () => randomBytes(16).toString('hex'),
): FetchGrantMinter {
  return (host, url) => {
    const expires = Math.floor(now() / 1000) + FETCH_GRANT_TTL_SECONDS
    const digest = createHash('sha256').update(url, 'utf8').digest('hex')
    const body = Buffer.from(`asset-fetch-once\n${host}\n${digest}\n${expires}\n${nonce()}`, 'utf8')
    const mac = createHmac('sha256', key).update(body).digest()
    return `${body.toString('base64url')}.${mac.toString('base64url')}`
  }
}
