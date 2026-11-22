import type { LookupAddress } from 'node:dns'
import { request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { isIP } from 'node:net'
import { EgressError, type Resolver, systemResolver } from '../../http/egress.js'
import { plainAddress } from '../../http/origins.js'
import { assertEndpointAllowed, PluginError } from '../registry.js'

// Downloading a plugin package's file (#6935, spec 2026-11-21 §6.6): a `marketplace.json`
// from a bare URL, an `archive` zip, an npm registry document or tarball. EVERY HOP goes
// through the egress check (`assertEndpointAllowed`: https, or http to a loopback or
// private address; never a link-local or cloud metadata host), and the connection is
// pinned to the addresses that check passed, so a name re-pointed between the check and
// the connect (DNS rebinding) cannot reach a refused address (as http/egress.ts
// `egressGetJson`). A redirect is followed only to a URL that passes the same check, at
// most `MAX_REDIRECTS` times. No credential is ever sent. The body is capped as it
// streams, and the whole download has one deadline.
//
// Claude Code's own limits (https://code.claude.com/docs/en/plugins/host-marketplace.md,
// "Stay within the download limits for hosted files"): a `marketplace.json` at most
// 5 MiB in 10 s; an archive at most 256 MiB in 120 s, with at most five redirects.

export const MAX_REDIRECTS = 5
export const MARKETPLACE_LIMITS = { maxBytes: 5 * 1024 * 1024, timeoutMs: 10_000 }
export const ARCHIVE_LIMITS = { maxBytes: 256 * 1024 * 1024, timeoutMs: 120_000 }

export type DownloadOptions = {
  /** Names the file in errors: "the marketplace", "archive". */
  what: string
  maxBytes: number
  timeoutMs: number
  accept?: string
  resolve?: Resolver
  signal?: AbortSignal | undefined
}

export type Downloaded = { bytes: Buffer; url: string }

/** The download's interface, so tests (and the installer) can stand in for it. */
export type Downloader = (url: string, options: DownloadOptions) => Promise<Downloaded>

type LookupCallback = (err: NodeJS.ErrnoException | null, address: string | LookupAddress[], family?: number) => void

/** A URL a download may start from or be redirected to; throws EgressError otherwise. */
function checkedUrl(raw: string, what: string): URL {
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    throw new EgressError(`${what}: ${raw} is not a URL`)
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') throw new EgressError(`${what}: ${url.href} must use https`)
  if (url.username || url.password) throw new EgressError(`${what}: ${url.origin} must not carry credentials`)
  return url
}

type Hop = { status: number; location?: string; bytes?: Buffer }

function hop(url: URL, addresses: string[], options: DownloadOptions, signal: AbortSignal): Promise<Hop> {
  const pinned: LookupAddress[] = addresses.map((address) => {
    const plain = plainAddress(address)
    return { address: plain, family: isIP(plain) === 6 ? 6 : 4 }
  })
  const first = pinned[0]!
  const lookupPinned = (_host: string, opts: unknown, cb: LookupCallback): void => {
    if (typeof opts === 'object' && opts !== null && (opts as { all?: boolean }).all === true) cb(null, pinned)
    else cb(null, first.address, first.family)
  }
  const send = url.protocol === 'https:' ? httpsRequest : httpRequest
  const fail = (detail: string) => new PluginError(`${options.what} ${url.origin}${url.pathname}: ${detail}`, 502)
  return new Promise<Hop>((resolve, reject) => {
    let settled = false
    const settle = (err: Error | undefined, value?: Hop) => {
      if (settled) return
      settled = true
      signal.removeEventListener('abort', onAbort)
      if (err) {
        req.destroy()
        reject(err)
      } else resolve(value!)
    }
    const onAbort = () => settle(signal.reason instanceof Error && signal.reason.name !== 'TimeoutError'
      ? signal.reason
      : fail(`did not finish within ${options.timeoutMs} ms`))
    const req = send(
      url,
      {
        method: 'GET',
        lookup: lookupPinned as never,
        headers: { accept: options.accept ?? '*/*', 'user-agent': 'ScadBuddy-plugin-installer' },
      },
      (res) => {
        const status = res.statusCode ?? 0
        if (status >= 300 && status < 400) {
          res.resume()
          const location = res.headers.location
          settle(undefined, { status, ...(location ? { location } : {}) })
          return
        }
        if (status !== 200) {
          res.resume()
          settle(fail(`answered HTTP ${status}`))
          return
        }
        const declared = Number(res.headers['content-length'])
        if (Number.isFinite(declared) && declared > options.maxBytes) {
          settle(new PluginError(`${options.what} is larger than ${options.maxBytes} bytes`, 422))
          return
        }
        let size = 0
        const chunks: Buffer[] = []
        res.on('data', (chunk: Buffer) => {
          size += chunk.length
          if (size > options.maxBytes) settle(new PluginError(`${options.what} is larger than ${options.maxBytes} bytes`, 422))
          else chunks.push(chunk)
        })
        res.on('end', () => settle(undefined, { status, bytes: Buffer.concat(chunks) }))
        res.on('error', (err) => settle(fail(`failed: ${err.message}`)))
        res.on('aborted', () => settle(fail('was cut off')))
      },
    )
    req.on('error', (err) => settle(fail(`failed: ${err.message}`)))
    if (signal.aborted) onAbort()
    else signal.addEventListener('abort', onAbort, { once: true })
    req.end()
  })
}

/**
 * GETs `url` through the egress check at every hop, following at most `MAX_REDIRECTS`
 * redirects, within `timeoutMs` in all and at most `maxBytes`. Throws EgressError for a
 * refused URL, PluginError (502) when the server fails, (422) when the file is too large.
 */
export const egressDownload: Downloader = async (start, options) => {
  const deadline = AbortSignal.timeout(options.timeoutMs)
  const signal = options.signal ? AbortSignal.any([options.signal, deadline]) : deadline
  const resolve = options.resolve ?? systemResolver
  let url = checkedUrl(start, options.what)
  for (let redirects = 0; ; redirects++) {
    let addresses: string[]
    try {
      addresses = await assertEndpointAllowed(url.href, resolve)
    } catch (err) {
      if (err instanceof EgressError) throw new EgressError(`${options.what}: ${err.message}`)
      throw err
    }
    const answer = await hop(url, addresses, options, signal)
    if (answer.bytes) return { bytes: answer.bytes, url: url.href }
    if (!answer.location) throw new PluginError(`${options.what} ${url.origin}: a redirect with no Location`, 502)
    if (redirects >= MAX_REDIRECTS) throw new PluginError(`${options.what}: more than ${MAX_REDIRECTS} redirects`, 502)
    url = checkedUrl(new URL(answer.location, url).href, `${options.what} (redirected)`)
  }
}
