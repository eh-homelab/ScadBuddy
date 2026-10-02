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
// A model's own image (#951), any path under its folder with an image extension in
// any case, as `GET /models/{slug}/images/{path}` serves.
const MODEL_IMAGE_ROUTE = new RegExp(`^/api/v1/models/${SEG}/images/(?:${SEG}/)*${SEG}$`)

/** The model a Markdown text belongs to, which its relative images resolve against (#951). */
export interface ImageBase {
  slug: string
  /** The revision being viewed; without one, the model's current files. */
  revision?: string
}

/**
 * The longest `data:` image shown (#951), in characters: about 384 KiB of base64. An
 * inline blob past it is its alt text, so one huge image cannot stall the chat.
 */
export const MAX_DATA_IMAGE_CHARS = 512 * 1024

// Inert as an `<img>` source. An SVG loaded as an image is processed in the HTML
// spec's secure mode: no script, and no external resource of any kind (an
// `<image href>`, a CSS `url()` or `@import`, a font). So an SVG data: image cannot
// make the browser fetch another host either.
const DATA_IMAGE = /^data:image\/(?:png|jpeg|gif|webp|svg\+xml)(?:;[^,;]+)*,/i

// What `GET /models/{slug}/images/{path}` serves.
const IMAGE_EXTENSION = /\.(?:png|jpe?g|gif|webp|svg)$/i

const ORIGIN = 'http://scadbuddy.invalid'

/**
 * A `src` for an image in untrusted Markdown (#820, #951), normalised, else null:
 *
 * - a same-origin path to one of ScadBuddy's image routes (a render view, colour map,
 *   output or plate thumbnail, model thumbnail or model image), so text from the
 *   model, a README, a preset description or a tool result can never make the browser
 *   fetch another host, or any other API route. A backslash is refused because the URL
 *   parser reads it as `/`, and `..` is resolved before the route is matched so it
 *   cannot climb to another route.
 * - a `data:image/*` of a type an `<img>` shows, up to `MAX_DATA_IMAGE_CHARS`.
 * - with a `base`, a path relative to that model's folder (`thumbnail.png`,
 *   `images/a.png`), as the model image route at the base's revision. The rules are
 *   the route's: nothing that climbs out, no dot-file segment, an image extension.
 *   Without a base (agent chat) such a path, like a filesystem path, is never fetched.
 */
export function safeImageSrc(value: string | null | undefined, base?: ImageBase): string | null {
  if (!value || value.includes('\\')) return null
  if (value.startsWith('/api/v1/')) return apiPath(value)
  if (DATA_IMAGE.test(value)) return value.length <= MAX_DATA_IMAGE_CHARS ? value : null
  return base ? modelImage(value, base) : null
}

function apiPath(value: string): string | null {
  let parsed: URL
  try {
    parsed = new URL(value, ORIGIN)
  } catch {
    return null
  }
  const path = parsed.pathname
  const allowed = IMAGE_ROUTES.test(path) || (MODEL_IMAGE_ROUTE.test(path) && IMAGE_EXTENSION.test(path))
  if (parsed.origin !== ORIGIN || !allowed) return null
  return parsed.pathname + parsed.search
}

function modelImage(value: string, base: ImageBase): string | null {
  // No scheme (which covers `C:/`), no absolute or protocol-relative path, and no
  // query or fragment: the route takes a plain file path and nothing else.
  if (/^[a-z][a-z0-9+.-]*:/i.test(value) || value.startsWith('/') || /[?#]/.test(value)) return null
  let parsed: URL
  try {
    parsed = new URL(value, `${ORIGIN}/m/`)
  } catch {
    return null
  }
  if (parsed.origin !== ORIGIN || !parsed.pathname.startsWith('/m/')) return null
  const path = parsed.pathname.slice('/m/'.length)
  const segments = path.split('/')
  for (const segment of segments) {
    let decoded: string
    try {
      decoded = decodeURIComponent(segment)
    } catch {
      return null
    }
    if (!decoded || decoded.startsWith('.') || /[/\\]/.test(decoded)) return null
  }
  if (!IMAGE_EXTENSION.test(decodeURIComponent(segments[segments.length - 1] ?? ''))) return null
  const revision = base.revision ? `?commit=${encodeURIComponent(base.revision)}` : ''
  return `/api/v1/models/${encodeURIComponent(base.slug)}/images/${path}${revision}`
}
