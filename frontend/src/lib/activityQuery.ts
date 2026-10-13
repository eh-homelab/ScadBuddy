/**
 * The Activity page's filters, kept in the URL
 * (`?kind=&status=&principal=&tool=&subject=&since=&q=&facet.<name>=`) so back/forward and
 * deep links work, as the print history's are (`printsQuery.ts`). `tool` is the API's
 * `via_tool`, and a `facet.<name>` key is a filterable facet of the selected kind.
 */
export type ActivityQuery = {
  kind: string
  status: string
  principal: string
  tool: string
  subject: string
  since: string
  q: string
  facets: Record<string, string>
}

export const DEFAULT_ACTIVITY_QUERY: ActivityQuery = {
  kind: '',
  status: '',
  principal: '',
  tool: '',
  subject: '',
  since: '',
  q: '',
  facets: {},
}

const FIXED = ['kind', 'status', 'principal', 'tool', 'subject', 'since', 'q'] as const
const FACET_PREFIX = 'facet.'

export function parseActivityQuery(params: URLSearchParams): ActivityQuery {
  const facets: Record<string, string> = {}
  for (const [key, value] of params) {
    if (key.startsWith(FACET_PREFIX) && key.length > FACET_PREFIX.length && value !== '') {
      facets[key.slice(FACET_PREFIX.length)] = value
    }
  }
  return {
    kind: params.get('kind') ?? '',
    status: params.get('status') ?? '',
    principal: params.get('principal') ?? '',
    tool: params.get('tool') ?? '',
    subject: params.get('subject') ?? '',
    since: params.get('since') ?? '',
    q: params.get('q') ?? '',
    facets,
  }
}

/** The query as URL parameters, leaving out every default. */
export function toActivityParams(query: ActivityQuery): URLSearchParams {
  const params = new URLSearchParams()
  for (const key of FIXED) {
    const value = query[key]
    if (key === 'q' ? value.trim() : value) params.set(key, value)
  }
  for (const [name, value] of Object.entries(query.facets)) {
    if (value !== '') params.set(`${FACET_PREFIX}${name}`, value)
  }
  return params
}

export function isActivityFiltered(query: ActivityQuery): boolean {
  return toActivityParams(query).size > 0
}

/** The query as `GET /api/v1/activity` parameters (`tool` is `via_tool`). A `kind` or
 * `status` holding several values is comma-joined in the URL, so the client splits it
 * into repeated keys in `client.ts`. */
export function apiParams(query: ActivityQuery): Record<string, string> {
  const out: Record<string, string> = {}
  if (query.kind) out.kind = query.kind
  if (query.status) out.status = query.status
  if (query.principal) out.principal = query.principal
  if (query.tool) out.via_tool = query.tool
  if (query.subject) out.subject = query.subject
  if (query.since) out.since = query.since
  if (query.q.trim()) out.q = query.q.trim()
  for (const [name, value] of Object.entries(query.facets)) {
    if (value !== '') out[`${FACET_PREFIX}${name}`] = value
  }
  return out
}
