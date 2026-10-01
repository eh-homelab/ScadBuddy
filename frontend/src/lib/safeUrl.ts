/**
 * An `href` that is safe to render: an absolute `http:` or `https:` URL, else null.
 *
 * `origin_url` is only ever set by the server's URL import, which fetches over
 * https. This is the second line anyway: a `javascript:` or `data:` value that
 * reached a record some other way (old data, a future write path) would otherwise
 * run in this origin at the click of a catalogue card's link.
 */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    return null
  }
  // `URL` lower-cases the scheme, so `JAVASCRIPT:` is caught here too.
  return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? value : null
}

/**
 * A `src` for an image in untrusted Markdown (#820): a same-origin path under
 * `/api/v1/`, normalised, else null. Only ScadBuddy's own API serves what such an
 * image may show (a render view, a thumbnail), so text from the model, a README or a
 * tool result can never make the browser fetch another host. A backslash is refused
 * because the URL parser reads it as `/`, and `..` is resolved before the prefix is
 * checked so it cannot climb out of the API.
 */
export function safeImageSrc(value: string | null | undefined): string | null {
  if (!value || !value.startsWith('/api/v1/') || value.includes('\\')) return null
  let parsed: URL
  try {
    parsed = new URL(value, 'http://scadbuddy.invalid')
  } catch {
    return null
  }
  if (parsed.origin !== 'http://scadbuddy.invalid' || !parsed.pathname.startsWith('/api/v1/')) return null
  return parsed.pathname + parsed.search
}
