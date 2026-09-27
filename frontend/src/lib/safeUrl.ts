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
