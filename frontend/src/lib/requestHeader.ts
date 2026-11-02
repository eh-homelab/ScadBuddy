// #4975 — the CSRF request header (spec 2026-11-01 §5.3 layer 3, D11): every request the
// page makes to its own API carries `X-ScadBuddy-Request: 1`, which another site's page
// could only send after a CORS preflight the API never answers. Installed once, around
// `window.fetch`, so every caller has it: the API client, the 3D loaders, template UIs and
// the trace relay. What the browser loads without script (an `<img>`, a download link)
// cannot carry it; the backend lists those (`auth/routes.py` `SCRIPTLESS_GETS`).

export const REQUEST_HEADER = 'X-ScadBuddy-Request'

/** Whether a request to `url` goes to this page's own API. */
export function needsHeader(url: string, origin = window.location.origin): boolean {
  let target: URL
  try {
    target = new URL(url, origin)
  } catch {
    return false
  }
  return target.origin === origin && (target.pathname.startsWith('/api/') || target.pathname.startsWith('/telemetry/'))
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === 'string') return input
  if (input instanceof URL) return input.href
  return input.url
}

export function withRequestHeader(fetchImpl: typeof fetch): typeof fetch {
  return (input, init) => {
    if (!needsHeader(urlOf(input))) return fetchImpl(input, init)
    if (input instanceof Request && init === undefined) {
      if (input.headers.has(REQUEST_HEADER)) return fetchImpl(input)
      const headers = new Headers(input.headers)
      headers.set(REQUEST_HEADER, '1')
      return fetchImpl(new Request(input, { headers }))
    }
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined))
    if (!headers.has(REQUEST_HEADER)) headers.set(REQUEST_HEADER, '1')
    return fetchImpl(input, { ...init, headers })
  }
}

let installed = false

export function installRequestHeader(): void {
  if (installed) return
  installed = true
  window.fetch = withRequestHeader(window.fetch.bind(window))
}
