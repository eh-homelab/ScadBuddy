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

/** The response body, or a ToolError naming `what` failed, the status and the backend's reason. */
export async function ok<T>(pending: Promise<FetchResult<T>>, what: string): Promise<T> {
  const { data, error, response } = await pending
  if (!response.ok) {
    const reason = describe(error)
    throw new ToolError(`${what} failed (HTTP ${response.status})${reason ? `: ${reason}` : ''}`)
  }
  // 204 responses have no body; openapi-fetch gives `{}` or undefined.
  return data as T
}
