import { createHash, randomBytes } from 'node:crypto'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { type CryptoKey, exportJWK, generateKeyPair, type JWK, type JWTPayload, SignJWT } from 'jose'

// A local identity provider for the OIDC tests (#262): an OAuth 2.1
// authorization server on 127.0.0.1 with RFC 8414 and OIDC discovery, a JWKS,
// dynamic client registration (RFC 7591), an authorization endpoint that
// consents at once, and a token endpoint that checks PKCE (S256) and puts the
// RFC 8707 `resource` in `aud`. Keys are generated per test run; nothing is
// fixed or checked in. Plain http is fine: the agent allows http for loopback
// only (src/http/egress.ts `assertSecureUrl`).

export type SigningKey = { kid: string; alg: string; privateKey: CryptoKey; publicJwk: JWK }

export async function newKey(alg: string, kid = `${alg.toLowerCase()}-${randomBytes(4).toString('hex')}`): Promise<SigningKey> {
  const { privateKey, publicKey } = await generateKeyPair(alg, { extractable: true })
  const publicJwk = { ...(await exportJWK(publicKey)), kid, alg, use: 'sig' }
  return { kid, alg, privateKey, publicJwk }
}

type Grant = { clientId: string; redirectUri: string; challenge: string; scope: string; resource: string | undefined }

export type FakeIdp = {
  /** `http://127.0.0.1:<port>`, exactly as `iss` and the metadata's `issuer` carry it. */
  issuer: string
  /** The keys the JWKS publishes; tests may replace them (rotation). */
  published: SigningKey[]
  /** The default signing key (published). */
  key: SigningKey
  hits: { metadata: number; jwks: number; token: number; register: number }
  /** Override what the metadata says the issuer is (for the mismatch test). */
  metadataIssuer: string | undefined
  /** Scopes the token endpoint grants, whatever the client asked (default: what it asked). */
  grantScope: string | undefined
  /** A signed access token: iss, sub, aud, scope, iat, exp by default, then `claims` on top. */
  sign(claims?: JWTPayload & { exp?: number }, options?: { key?: SigningKey; typ?: string; omit?: string[] }): Promise<string>
  close(): Promise<void>
}

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = []
  for await (const chunk of req) chunks.push(chunk as Buffer)
  return Buffer.concat(chunks).toString('utf8')
}

function json(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { 'content-type': 'application/json', 'access-control-allow-origin': '*' })
  res.end(JSON.stringify(body))
}

export async function startFakeIdp(options: { audience?: string; alg?: string } = {}): Promise<FakeIdp> {
  const key = await newKey(options.alg ?? 'RS256')
  const grants = new Map<string, Grant>()
  const server: Server = createServer()
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const issuer = `http://127.0.0.1:${(server.address() as AddressInfo).port}`

  const idp: FakeIdp = {
    issuer,
    published: [key],
    key,
    hits: { metadata: 0, jwks: 0, token: 0, register: 0 },
    metadataIssuer: undefined,
    grantScope: undefined,
    async sign(claims = {}, opts = {}) {
      const signer = opts.key ?? key
      const now = Math.floor(Date.now() / 1000)
      const payload: JWTPayload = {
        iss: issuer,
        sub: 'alice',
        aud: options.audience ?? 'https://scadbuddy.test/mcp',
        scope: 'scadbuddy:read',
        iat: now,
        exp: now + 300,
        ...claims,
      }
      for (const name of opts.omit ?? []) delete payload[name]
      return new SignJWT(payload)
        .setProtectedHeader({ alg: signer.alg, kid: signer.kid, typ: opts.typ ?? 'at+jwt' })
        .sign(signer.privateKey)
    },
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  }

  const metadata = () => ({
    issuer: idp.metadataIssuer ?? issuer,
    authorization_endpoint: `${issuer}/authorize`,
    token_endpoint: `${issuer}/token`,
    registration_endpoint: `${issuer}/register`,
    jwks_uri: `${issuer}/jwks`,
    response_types_supported: ['code'],
    grant_types_supported: ['authorization_code'],
    code_challenge_methods_supported: ['S256'],
    token_endpoint_auth_methods_supported: ['none'],
    scopes_supported: ['openid', 'scadbuddy:read', 'scadbuddy:write', 'scadbuddy:outward'],
  })

  server.on('request', (req, res) => {
    void (async () => {
      const url = new URL(req.url ?? '/', issuer)
      switch (`${req.method} ${url.pathname}`) {
        case 'GET /.well-known/oauth-authorization-server':
        case 'GET /.well-known/openid-configuration':
          idp.hits.metadata++
          return json(res, 200, metadata())
        case 'GET /jwks':
          idp.hits.jwks++
          return json(res, 200, { keys: idp.published.map((k) => k.publicJwk) })
        case 'POST /register': {
          idp.hits.register++
          const body = JSON.parse(await readBody(req)) as Record<string, unknown>
          return json(res, 201, { ...body, client_id: `client-${randomBytes(4).toString('hex')}`, token_endpoint_auth_method: 'none' })
        }
        case 'GET /authorize': {
          const p = url.searchParams
          if (p.get('response_type') !== 'code' || p.get('code_challenge_method') !== 'S256') return json(res, 400, { error: 'invalid_request' })
          const code = randomBytes(16).toString('hex')
          grants.set(code, {
            clientId: p.get('client_id') ?? '',
            redirectUri: p.get('redirect_uri') ?? '',
            challenge: p.get('code_challenge') ?? '',
            scope: p.get('scope') ?? '',
            resource: p.get('resource') ?? undefined,
          })
          const to = new URL(p.get('redirect_uri') ?? '')
          to.searchParams.set('code', code)
          if (p.get('state')) to.searchParams.set('state', p.get('state')!)
          res.writeHead(302, { location: to.href })
          return res.end()
        }
        case 'POST /token': {
          idp.hits.token++
          const p = new URLSearchParams(await readBody(req))
          const grant = grants.get(p.get('code') ?? '')
          grants.delete(p.get('code') ?? '')
          const verifier = p.get('code_verifier') ?? ''
          const challenge = createHash('sha256').update(verifier).digest('base64url')
          if (!grant || grant.challenge !== challenge || grant.clientId !== p.get('client_id') || grant.redirectUri !== p.get('redirect_uri')) {
            return json(res, 400, { error: 'invalid_grant' })
          }
          const resource = p.get('resource') ?? grant.resource
          if (!resource) return json(res, 400, { error: 'invalid_target', error_description: 'resource is required' })
          const scope = idp.grantScope ?? grant.scope
          const token = await idp.sign({ aud: resource, scope, client_id: grant.clientId })
          return json(res, 200, { access_token: token, token_type: 'Bearer', expires_in: 300, scope })
        }
        default:
          return json(res, 404, { error: 'not_found' })
      }
    })().catch((err: unknown) => json(res, 500, { error: String(err) }))
  })

  return idp
}
