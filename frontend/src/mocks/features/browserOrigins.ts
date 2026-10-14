import { HttpResponse, http } from 'msw'
import type { ApprovedBrowserOrigin, BrowserOriginsSetting } from '../../api/types'

/**
 * #982 — the headless browser's allow and deny lists and each chat's approved origins,
 * served by the agent service (agent/src/routes/browserOrigins.ts). The defaults are the
 * agent's (`src/browser/origins.ts`); the mock checks rules only loosely.
 */

const DEFAULTS = {
  allow: [
    'makerworld.com',
    'printables.com',
    'www.printables.com',
    'thingiverse.com',
    'www.thingiverse.com',
    'github.com',
    'raw.githubusercontent.com',
    'openscad.org',
    'en.wikibooks.org',
  ],
  deny: [
    'localhost',
    '*.localhost',
    '127.0.0.0/8',
    '::1/128',
    '0.0.0.0/8',
    '169.254.0.0/16',
    'fe80::/10',
    '100.100.100.200/32',
    'fd00:ec2::254/128',
    'metadata',
    'metadata.goog',
    '*.internal',
    '*.svc',
    '*.cluster.local',
  ],
}

const SEEDED: ApprovedBrowserOrigin[] = [
  {
    session_id: 'session-1',
    title: 'Spool lock from MakerWorld',
    origin: 'https://cults3d.com',
    scope: 'session',
    approved_at: '2026-10-14T09:12:00Z',
  },
]

const state = { allow: [...DEFAULTS.allow], deny: [...DEFAULTS.deny], approved: [...SEEDED] }

export function reset(): void {
  state.allow = [...DEFAULTS.allow]
  state.deny = [...DEFAULTS.deny]
  state.approved = [...SEEDED]
}

const view = (): BrowserOriginsSetting => ({
  allow: state.allow,
  deny: state.deny,
  defaults: { allow: [...DEFAULTS.allow], deny: [...DEFAULTS.deny] },
  approved: state.approved,
})

/** The agent's message for a rule that is not one, for the few shapes the mock refuses. */
function badRule(rules: string[]): string | undefined {
  return rules.find((r) => !r.trim() || /\s/.test(r.trim()) || /:\/\/[^/]+\/./.test(r))
}

export const handlers = [
  http.get('/api/v1/ai/settings/browser-origins', () => HttpResponse.json(view())),
  http.put('/api/v1/ai/settings/browser-origins', async ({ request }) => {
    const body = (await request.json()) as { allow?: string[]; deny?: string[] }
    const bad = badRule([...(body.allow ?? []), ...(body.deny ?? [])])
    if (bad !== undefined) return HttpResponse.json({ detail: `"${bad}" is not a host, an origin or an address range` }, { status: 400 })
    state.allow = [...new Set((body.allow ?? []).map((r) => r.trim()))]
    state.deny = [...new Set((body.deny ?? []).map((r) => r.trim()))]
    return HttpResponse.json(view())
  }),
  http.post('/api/v1/ai/settings/browser-origins/allow', async ({ request }) => {
    const { rule } = (await request.json()) as { rule: string }
    if (!state.allow.includes(rule)) state.allow = [...state.allow, rule]
    return HttpResponse.json(view())
  }),
  http.post('/api/v1/ai/sessions/:id/browser-origins', async ({ params, request }) => {
    const { origin } = (await request.json()) as { origin: string }
    const id = String(params.id)
    state.approved = [
      { session_id: id, title: 'This chat', origin, scope: 'session', approved_at: new Date().toISOString() },
      ...state.approved.filter((a) => !(a.session_id === id && a.origin === origin)),
    ]
    return HttpResponse.json({ origin, scope: 'session' })
  }),
  http.delete('/api/v1/ai/sessions/:id/browser-origins', ({ params, request }) => {
    const origin = new URL(request.url).searchParams.get('origin')
    const before = state.approved.length
    state.approved = state.approved.filter((a) => !(a.session_id === String(params.id) && a.origin === origin))
    if (state.approved.length === before) return HttpResponse.json({ detail: 'not approved' }, { status: 404 })
    return new HttpResponse(null, { status: 204 })
  }),
]
