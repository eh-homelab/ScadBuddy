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

// The read-only, side-effect-free image routes Markdown may embed. Nothing else under
// /api/v1/ is: an <img> fires a cookie-carrying GET on render, with no click.
const SEG = '[A-Za-z0-9._~%-]+'
const IMAGE_ROUTES = new RegExp(
  `^/api/v1/(?:jobs/${SEG}/(?:views/${SEG}\\.png|colours\\.png)` +
    `|outputs/${SEG}/(?:thumbnail|views/${SEG}\\.png|plates/\\d+/thumbnail)` +
    `|models/${SEG}/thumbnail)$`,
)

/**
 * A `src` for an image in untrusted Markdown (#820): a same-origin path to one of
 * ScadBuddy's image routes (a render view, colour map, output or plate thumbnail, or
 * model thumbnail), normalised, else null. Text from the model, a README, a preset
 * description or a tool result can never make the browser fetch another host, or any
 * other API route. A backslash is refused because the URL parser reads it as `/`, and
 * `..` is resolved before the route is matched so it cannot climb to another route.
 */
export function safeImageSrc(value: string | null | undefined): string | null {
  if (!value || !value.startsWith('/api/v1/') || value.includes('\\')) return null
  let parsed: URL
  try {
    parsed = new URL(value, 'http://scadbuddy.invalid')
  } catch {
    return null
  }
  if (parsed.origin !== 'http://scadbuddy.invalid' || !IMAGE_ROUTES.test(parsed.pathname)) return null
  return parsed.pathname + parsed.search
}
