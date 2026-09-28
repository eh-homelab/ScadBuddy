import createClient from 'openapi-fetch'
import type { paths } from './schema.js'

// Typed client for the Python backend (spec §4.3). `schema.d.ts` is not
// committed (#492): `pnpm gen:api` writes it from the backend's exported spec
// before every typecheck, test and build. So a backend route or model change
// that breaks this client fails the PR's `agent` job.

export type BackendClient = ReturnType<typeof createClient<paths>>

export function createBackendClient(
  baseUrl: string,
  fetchImpl: typeof fetch = globalThis.fetch,
): BackendClient {
  return createClient<paths>({ baseUrl, fetch: (request) => fetchImpl(request) })
}

/** True when the backend's own /healthz answers 2xx within the timeout. Never throws. */
export async function backendReachable(client: BackendClient, timeoutMs = 2000): Promise<boolean> {
  try {
    const { response } = await client.GET('/healthz', { signal: AbortSignal.timeout(timeoutMs) })
    return response.ok
  } catch {
    return false
  }
}
