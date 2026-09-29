import { HttpResponse, http } from 'msw'
import { OIDC_API, type OidcConfig, type OidcView } from '../components/McpOidcSettings'

// The agent service's /api/v1/ai/mcp/oidc (#262, agent/src/routes/mcpAuth.ts)
// for vitest and the mocked build. Discovery "passes" for any https issuer
// except one containing "typo", which is how the tests exercise a refusal.

const RESOURCE = 'https://scadbuddy.example/mcp'

const DEFAULTS: OidcConfig = {
  enabled: false,
  issuer: 'https://idp.example.com/',
  audience: null,
  client_id: null,
  scopes: { read: 'scadbuddy:read', write: 'scadbuddy:write', outward: 'scadbuddy:outward' },
  tier_claim: null,
  algorithms: ['RS256', 'ES256'],
}

let stored: OidcConfig | undefined

export function resetMcpOidcMock(): void {
  stored = undefined
}

function view(): OidcView {
  return {
    config: stored ?? DEFAULTS,
    saved: stored !== undefined,
    resource: RESOURCE,
    resource_metadata_url: 'https://scadbuddy.example/.well-known/oauth-protected-resource',
    supported_algorithms: ['RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512', 'ES256', 'ES384', 'ES512', 'EdDSA'],
    can_enable: true,
    cannot_enable_reason: null,
  }
}

function discovery(issuer: string) {
  if (!issuer.startsWith('https://') || issuer.includes('typo')) {
    return { detail: `discovery against ${issuer} failed: no metadata found` }
  }
  return { issuer, jwks_uri: `${issuer.replace(/\/$/, '')}/jwks`, keys: 1, dynamic_registration: true }
}

export const mcpOidcHandlers = [
  http.get(OIDC_API, () => HttpResponse.json(view())),
  http.put(OIDC_API, async ({ request }) => {
    const config = (await request.json()) as OidcConfig
    if (config.enabled) {
      const found = discovery(config.issuer)
      if ('detail' in found) {
        return HttpResponse.json({ detail: `OIDC was not enabled (nothing was saved): ${found.detail}` }, { status: 400 })
      }
    }
    stored = config
    return HttpResponse.json(view())
  }),
  http.post(`${OIDC_API}/test`, async ({ request }) => {
    const { issuer } = (await request.json()) as { issuer: string }
    const found = discovery(issuer)
    return HttpResponse.json(found, { status: 'detail' in found ? 400 : 200 })
  }),
]
