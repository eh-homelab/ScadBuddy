// Free of Node imports, so the session workflow's bundle can use it (gate/ids.ts).
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** A canonical 8-4-4-4-12 hex UUID: every session id (and so directory name) is one. */
export function isUuid(value: string): boolean {
  return UUID.test(value)
}
