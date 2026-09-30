import { request as httpRequest, type OutgoingHttpHeaders, type RequestOptions } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { isIP, type LookupFunction } from 'node:net'
import { plainAddress } from './origins.js'

// Requests to an address the egress check already passed (plugins/registry.ts
// `assertEndpointAllowed`), never resolving the name again: the connection goes
// to `address`, while TLS is still verified against the URL's hostname (SNI
// and certificate). The plugin forwarder (plugins/forwarder.ts) builds its
// upstream requests with `pinnedRequestOptions`; `pinnedFetch` is the same
// for a plain request/response call (memory/hindsight.ts), and follows no
// redirect.

/** Node request options for `target` that connect to `address` only. */
export function pinnedRequestOptions(target: URL, address: string): RequestOptions & { servername?: string } {
  const hostname = target.hostname.startsWith('[') ? target.hostname.slice(1, -1) : target.hostname
  const pinned = plainAddress(address)
  const family = isIP(pinned) === 6 ? 6 : 4
  // Pinned: the checked address, whatever the name resolves to now.
  const lookup = ((_host: string, opts: { all?: boolean }, cb: (...args: unknown[]) => void) => {
    if (opts.all) cb(null, [{ address: pinned, family }])
    else cb(null, pinned, family)
  }) as unknown as LookupFunction
  return {
    protocol: target.protocol,
    hostname,
    port: target.port || (target.protocol === 'https:' ? 443 : 80),
    path: `${target.pathname}${target.search}`,
    lookup,
    agent: false,
    ...(target.protocol === 'https:' && isIP(hostname) === 0 ? { servername: hostname } : {}),
  }
}

/** `http.request` or `https.request`, by the target's scheme. */
export function requestFor(target: URL): typeof httpRequest {
  return target.protocol === 'https:' ? (httpsRequest as unknown as typeof httpRequest) : httpRequest
}

export type PinnedResponse = { status: number; body: string }

export class PinnedRequestError extends Error {
  override name = 'PinnedRequestError'
}

/**
 * One request to `url` at the pinned `address`. A 30x is an error (redirects
 * are not followed), as is a body over `maxBytes`. Aborted by `signal`.
 */
export function pinnedFetch(
  url: string,
  address: string,
  init: { method: string; headers?: OutgoingHttpHeaders; body?: string; signal?: AbortSignal; maxBytes?: number },
): Promise<PinnedResponse> {
  const target = new URL(url)
  const body = init.body === undefined ? undefined : Buffer.from(init.body, 'utf8')
  const headers: OutgoingHttpHeaders = { ...init.headers, host: target.host }
  if (body) headers['content-length'] = body.length
  const maxBytes = init.maxBytes ?? 4 * 1024 * 1024
  return new Promise((resolve, reject) => {
    const req = requestFor(target)({
      ...pinnedRequestOptions(target, address),
      method: init.method,
      headers,
      ...(init.signal ? { signal: init.signal } : {}),
    })
    req.on('error', reject)
    req.on('response', (res) => {
      const status = res.statusCode ?? 0
      if (status >= 300 && status < 400) {
        res.resume()
        reject(new PinnedRequestError(`HTTP ${status} (a redirect); redirects are not followed`))
        return
      }
      const chunks: Buffer[] = []
      let size = 0
      res.on('data', (chunk: Buffer) => {
        size += chunk.length
        if (size > maxBytes) {
          res.destroy()
          reject(new PinnedRequestError(`response body over ${maxBytes} bytes`))
          return
        }
        chunks.push(chunk)
      })
      res.on('end', () => resolve({ status, body: Buffer.concat(chunks).toString('utf8') }))
      res.on('error', reject)
    })
    req.end(body)
  })
}
