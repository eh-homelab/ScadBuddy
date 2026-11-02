import { createHash, randomBytes } from 'node:crypto'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { type FakeMcp, startFakeMcp } from './fakeMcp.js'

// A local OAuth 2.1 authorization server and an MCP server it protects, for the plugin
// sign-in tests (#780). Never a real provider.
//
// The authorization server publishes RFC 8414 metadata at its well-known URL, registers
// clients (RFC 7591) when `registration` is on, answers /authorize with a redirect to the
// client's redirect_uri carrying a code, the state and `iss` (RFC 9207), and checks at
// /token the code, the PKCE verifier (S256), the redirect URI, the client and `resource`.
// Refresh tokens rotate; a revoked one is `invalid_grant`. `githubErrors` answers token
// errors with HTTP 200, as GitHub does.
//
// The MCP server (fakeMcp.ts) answers 401 with `WWW-Authenticate: Bearer resource_metadata=…`
// to any request without a live access token, and serves its RFC 9728 metadata.

export type FakeAuthServer = {
  issuer: string
  /** Each /authorize query, each /token form, each /register body. */
  authorizations: URLSearchParams[]
  tokenRequests: URLSearchParams[]
  tokenAuthorizations: (string | undefined)[]
  registrations: unknown[]
  /** Access tokens the MCP server accepts now. */
  live: Set<string>
  /** Every access token issued. */
  issued: string[]
  /** Refresh tokens that still work. */
  refresh: Set<string>
  /** Revokes every token: refresh becomes invalid_grant, and the MCP server refuses each access token. */
  revokeAll(): void
  close(): Promise<void>
}

export type FakeAuthOptions = {
  registration?: boolean
  /** `expires_in` of each access token; omitted, none is given. */
  expiresIn?: number
  /** Whether a refresh token is given. Default true. */
  refreshTokens?: boolean
  /** Advertise `authorization_response_iss_parameter_supported`. Default true. */
  issSupported?: boolean
  /** The `iss` sent back (default the issuer); null sends none. */
  iss?: string | null
  /** No S256 in code_challenge_methods_supported. */
  noPkce?: boolean
  /** Token errors as HTTP 200 with an `error` member (GitHub). */
  githubErrors?: boolean
  /** The issuer's path, e.g. `/login/oauth` (GitHub's). */
  issuerPath?: string
  /** A client the server knows (a pre-registered app): id and secret. */
  client?: { id: string; secret: string }
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    req.on('data', (c: Buffer) => chunks.push(c))
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

function json(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { 'content-type': 'application/json' }).end(JSON.stringify(body))
}

export async function startFakeAuthServer(options: FakeAuthOptions = {}): Promise<FakeAuthServer> {
  const path = options.issuerPath ?? ''
  const codes = new Map<string, { challenge: string; redirect: string; client: string; resource: string | null }>()
  const clients = new Map<string, string | undefined>()
  if (options.client) clients.set(options.client.id, options.client.secret)
  const out: Omit<FakeAuthServer, 'issuer' | 'close' | 'revokeAll'> = {
    authorizations: [],
    tokenRequests: [],
    tokenAuthorizations: [],
    registrations: [],
    live: new Set(),
    issued: [],
    refresh: new Set(),
  }
  let issuer = ''
  const tokenError = (res: ServerResponse, error: string) => json(res, options.githubErrors ? 200 : 400, { error, error_description: `fake: ${error}` })
  const issue = (res: ServerResponse) => {
    const access = `at-${randomBytes(12).toString('hex')}`
    out.live.add(access)
    out.issued.push(access)
    const body: Record<string, unknown> = { access_token: access, token_type: 'bearer', scope: 'mcp' }
    if (options.expiresIn !== undefined) body.expires_in = options.expiresIn
    if (options.refreshTokens !== false) {
      const refresh = `rt-${randomBytes(12).toString('hex')}`
      out.refresh.add(refresh)
      body.refresh_token = refresh
    }
    json(res, 200, body)
  }
  const server: Server = createServer((req, res) => {
    void (async () => {
      const url = new URL(req.url ?? '/', issuer || 'http://x')
      if (req.method === 'GET' && url.pathname === `/.well-known/oauth-authorization-server${path}`) {
        return json(res, 200, {
          issuer,
          authorization_endpoint: `${issuer}/authorize`,
          token_endpoint: `${issuer}/token`,
          ...(options.registration ? { registration_endpoint: `${issuer}/register` } : {}),
          response_types_supported: ['code'],
          ...(options.noPkce ? {} : { code_challenge_methods_supported: ['S256'] }),
          ...(options.issSupported === false ? {} : { authorization_response_iss_parameter_supported: true }),
        })
      }
      if (req.method === 'POST' && url.pathname === `${path}/register`) {
        const body = JSON.parse(await readBody(req)) as unknown
        out.registrations.push(body)
        const id = `client-${randomBytes(6).toString('hex')}`
        clients.set(id, undefined)
        return json(res, 201, { client_id: id, token_endpoint_auth_method: 'none' })
      }
      if (req.method === 'GET' && url.pathname === `${path}/authorize`) {
        out.authorizations.push(url.searchParams)
        const q = url.searchParams
        const client = q.get('client_id') ?? ''
        if (!clients.has(client) || q.get('code_challenge_method') !== 'S256') return json(res, 400, { error: 'invalid_request' })
        const code = `code-${randomBytes(8).toString('hex')}`
        codes.set(code, {
          challenge: q.get('code_challenge') ?? '',
          redirect: q.get('redirect_uri') ?? '',
          client,
          resource: q.get('resource'),
        })
        const back = new URL(q.get('redirect_uri') ?? '')
        back.searchParams.set('code', code)
        back.searchParams.set('state', q.get('state') ?? '')
        if (options.iss !== null) back.searchParams.set('iss', options.iss ?? issuer)
        res.writeHead(302, { location: back.toString() }).end()
        return
      }
      if (req.method === 'POST' && url.pathname === `${path}/token`) {
        const form = new URLSearchParams(await readBody(req))
        out.tokenRequests.push(form)
        out.tokenAuthorizations.push(req.headers.authorization)
        const client = form.get('client_id') ?? ''
        if (!clients.has(client)) return tokenError(res, 'invalid_client')
        const secret = clients.get(client)
        if (secret !== undefined && form.get('client_secret') !== secret) return tokenError(res, 'invalid_client')
        if (!form.get('resource')) return tokenError(res, 'invalid_target')
        if (form.get('grant_type') === 'authorization_code') {
          const held = codes.get(form.get('code') ?? '')
          codes.delete(form.get('code') ?? '')
          if (!held || held.client !== client || held.redirect !== form.get('redirect_uri')) return tokenError(res, 'invalid_grant')
          const challenge = createHash('sha256').update(form.get('code_verifier') ?? '').digest('base64url')
          if (challenge !== held.challenge || held.resource !== form.get('resource')) return tokenError(res, 'invalid_grant')
          return issue(res)
        }
        if (form.get('grant_type') === 'refresh_token') {
          const token = form.get('refresh_token') ?? ''
          if (!out.refresh.has(token)) return tokenError(res, options.githubErrors ? 'bad_refresh_token' : 'invalid_grant')
          out.refresh.delete(token)
          return issue(res)
        }
        return tokenError(res, 'unsupported_grant_type')
      }
      res.writeHead(404).end()
    })().catch(() => {
      if (!res.headersSent) res.writeHead(500).end()
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  issuer = `http://127.0.0.1:${(server.address() as AddressInfo).port}${path}`
  return {
    ...out,
    issuer,
    revokeAll: () => {
      out.live.clear()
      out.refresh.clear()
    },
    close: () =>
      new Promise<void>((resolve) => {
        server.closeAllConnections()
        server.close(() => resolve())
      }),
  }
}

/**
 * An MCP server protected by `auth`: 401 with the challenge unless the bearer token is one of
 * `auth.live`; its RFC 9728 metadata at `/.well-known/oauth-protected-resource/mcp`. Every
 * Authorization header it saw is in `seen`.
 */
export async function startProtectedMcp(
  auth: Pick<FakeAuthServer, 'issuer' | 'live'>,
  options: { scope?: string; scopesSupported?: string[]; resource?: (url: string) => string } = {},
): Promise<FakeMcp & { seen: (string | undefined)[] }> {
  const seen: (string | undefined)[] = []
  let base = ''
  const mcp = await startFakeMcp({
    path: '/mcp/',
    intercept: (req, res) => {
      const path = (req.url ?? '').split('?')[0]
      if (req.method === 'GET' && path === '/.well-known/oauth-protected-resource/mcp') {
        const resource = `${base}/mcp`
        json(res, 200, {
          resource: options.resource ? options.resource(resource) : resource,
          authorization_servers: [auth.issuer],
          ...(options.scopesSupported ? { scopes_supported: options.scopesSupported } : {}),
        })
        return true
      }
      seen.push(req.headers.authorization)
      const token = /^Bearer (.+)$/.exec(req.headers.authorization ?? '')?.[1]
      if (token === undefined || !auth.live.has(token)) {
        const scope = options.scope ? `, scope="${options.scope}"` : ''
        res
          .writeHead(401, {
            'www-authenticate': `Bearer error="invalid_token", resource_metadata="${base}/.well-known/oauth-protected-resource/mcp"${scope}`,
          })
          .end()
        return true
      }
      return false
    },
  })
  base = new URL(mcp.url).origin
  return Object.assign(mcp, { seen })
}
