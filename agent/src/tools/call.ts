import { ToolError } from './registry.js'

// Turning an openapi-fetch result into data or a ToolError the model can read.
// The backend answers errors as problem details (`backend/scadbuddy/api`, and
// `bambuddy/errors.py` for Bambuddy calls, which names the missing API-key
// scope); FastAPI's own 422 carries a `detail` list. Both are passed through
// as text so the agent sees the backend's own explanation.

type FetchResult<T> = { data?: T; error?: unknown; response: Response }

function describe(error: unknown): string {
  if (error === undefined || error === null || error === '') return ''
  if (typeof error === 'string') return error
  if (typeof error === 'object') {
    const body = error as { detail?: unknown; title?: unknown }
    if (typeof body.detail === 'string') {
      return typeof body.title === 'string' ? `${body.title}: ${body.detail}` : body.detail
    }
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).join('.')}: ${d.msg ?? ''}`)
        .join('; ')
    }
  }
  return JSON.stringify(error)
}

/** ScadBuddy's own problem types; any other `type` (`about:blank`, a relayed one) has no slug. */
const PROBLEM_TYPE = /^https:\/\/scadbuddy\.dev\/problems\/(?:[a-z0-9_-]+\/)*([a-z0-9_-]+)$/

/** The slug a ScadBuddy problem's `type` ends in (`…/problems/orders/already_notified`), if any. */
function problemSlug(error: unknown): string | undefined {
  const type = typeof error === 'object' && error !== null ? (error as { type?: unknown }).type : undefined
  return typeof type === 'string' ? PROBLEM_TYPE.exec(type)?.[1] : undefined
}

/** A property key (backend `entities/properties.py` `KEY`): safe to name in a summary. */
const PROPERTY_KEY = /^[a-z][a-z0-9_]{0,63}$/

/**
 * A checked save's 422 `missing_required_properties` (#7703): the summary names each key, so
 * the model can fill them in one retry; each reason is the backend's text, so it stays untrusted.
 */
function missingRequired(what: string, status: number, error: unknown): ToolError | undefined {
  if (typeof error !== 'object' || error === null) return undefined
  const body = error as { code?: unknown; missing?: unknown; invalid?: unknown }
  if (body.code !== 'missing_required_properties') return undefined
  const missing = (Array.isArray(body.missing) ? body.missing : []).filter((k): k is string => typeof k === 'string' && PROPERTY_KEY.test(k))
  const invalid = (Array.isArray(body.invalid) ? body.invalid : []).filter(
    (i): i is { key: string; reason?: unknown } => typeof i === 'object' && i !== null && typeof i.key === 'string' && PROPERTY_KEY.test(i.key),
  )
  const parts = [
    missing.length ? `missing: ${missing.join(', ')}` : '',
    invalid.length ? `values that do not fit: ${invalid.map((i) => i.key).join(', ')}` : '',
  ].filter(Boolean)
  const reasons = invalid.map((i) => `${i.key}: ${typeof i.reason === 'string' ? i.reason : JSON.stringify(i.reason)}`).join('; ')
  return new ToolError(
    `${what} failed (HTTP ${status}): its required properties are not all filled (${parts.join('; ') || 'see the reason'}). ` +
      'Nothing was written; give each one a value in `properties`.',
    status,
    reasons || describe(error) || undefined,
    problemSlug(error),
  )
}

/** The response body, or a ToolError naming `what` failed, the status and the backend's reason. */
export async function ok<T>(pending: Promise<FetchResult<T>>, what: string): Promise<T> {
  const { data, error, response } = await pending
  if (!response.ok) {
    const refused = response.status === 422 ? missingRequired(what, response.status, error) : undefined
    if (refused) throw refused
    const reason = describe(error)
    // The reason is the backend's (or, relayed, Bambuddy's) text: untrusted (#258).
    throw new ToolError(`${what} failed (HTTP ${response.status})`, response.status, reason || undefined, problemSlug(error))
  }
  // 204 responses have no body; openapi-fetch gives `{}` or undefined.
  return data as T
}
