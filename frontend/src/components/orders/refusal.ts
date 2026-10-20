import { ApiError } from '../../api/client'

/** The orders lifecycle's problem types (`api/orders.py` `PROBLEMS`). */
export const ORDERS_PROBLEMS = 'https://scadbuddy.dev/problems/orders/'

/** The refusal's slug when it is the lifecycle's (`order_not_closable`), else null. */
export function refusalSlug(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null
  const type = error.problem.type ?? ''
  return type.startsWith(ORDERS_PROBLEMS) ? type.slice(ORDERS_PROBLEMS.length) : null
}

/** The ids a refusal names (`items`), for the page to show by title. */
export function refusalItems(error: unknown): string[] {
  if (!(error instanceof ApiError)) return []
  const items = (error.problem as { items?: unknown }).items
  return Array.isArray(items) ? items.filter((id): id is string => typeof id === 'string') : []
}

/** What to tell the person: the backend's detail, else the error's message. */
export function refusalText(error: unknown): string {
  if (error instanceof ApiError) {
    const errors = (error.problem as { errors?: { msg?: string }[] }).errors
    const first = errors?.[0]?.msg
    return first && error.status === 422 ? first : error.detail
  }
  return error instanceof Error ? error.message : String(error)
}
