// A path parameter of `.` or `..` (#7221). `encodeURIComponent` leaves dots as they are, and the
// URL then normalises the segment away, so the call reaches another route with no redirect:
// `/orders/o1/items/{item}/quote` with item `..` is sent as `/orders/o1/quote`, under the
// agent's service credential. A value with `/` is safe (`%2F`). The client's refusal of
// redirects (backend.ts, #7104) stays as a backstop for the last segment.

/** A backend path that names a `.` or `..` segment: it is never sent. */
export class DotSegmentError extends Error {
  override name = 'DotSegmentError'
}

/** `.` or `..`, as the URL parser reads them: `%2e` in either case is a dot too. */
const DOT_SEGMENT = /^(?:\.|%2e){1,2}$/i

/** Whether `value`, as one path segment, is `.` or `..`. */
export function isDotSegment(value: string): boolean {
  return DOT_SEGMENT.test(value)
}

/**
 * `url` (a full URL or a path) as it is, or DotSegmentError when its path names a `.` or `..`
 * segment. Its query and fragment are not read. `what` names the call; the message never holds the URL.
 */
export function refuseDotSegments(url: string, what: string): string {
  const path = url.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/?#]*/i, '').split(/[?#]/, 1)[0] ?? ''
  if (path.split('/').some(isDotSegment)) throw new DotSegmentError(`${what}: a path segment is "." or "..", so it was not sent`)
  return url
}

/**
 * openapi-fetch's `pathSerializer` check: the path `serialize` builds from `pathname` and its
 * parameters is refused when a segment is `.` or `..`, before any URL is made. It reads the
 * serialized path, so a parameter of any style (a string, an array of one `..`) is caught; a
 * `%2e` is not, since the serializer encodes its `%`.
 */
export function refusingDotParams(serialize: (pathname: string, params: Record<string, unknown>) => string) {
  return (pathname: string, params: Record<string, unknown>): string => refuseDotSegments(serialize(pathname, params), pathname)
}
