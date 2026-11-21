// The request-body defaults of the generated API types (#6602), shared by
// frontend/scripts/gen-api.mjs and agent/scripts/gen-api.mjs (#6660). The Dockerfile's
// `frontend` and `agent-build` stages copy it to /src/scripts/. It imports nothing:
// each caller passes its own `typescript`.
//
// openapi-typescript makes a property with a `default` required (`defaultNonNullable`),
// which is true of a response (the backend sends every field) but not of a request:
// FastAPI leaves a defaulted field out of `required`, and the body may leave it out.
// openapi-typescript skips an inline request body, but FastAPI puts every body behind a
// `$ref`. So a defaulted property of a schema that only request bodies reach is made
// optional. A schema that a response reaches too keeps the response's shape.
// Test: agent/test/genApiRequestDefaults.test.ts. Lint: agent's `pnpm lint` (#6671).

const SCHEMAS = '#/components/schemas/'

/** One JSON-pointer segment, unescaped (RFC 6901 §4: `~1` first, then `~0`). */
export function unescapePointer(segment) {
  return segment.replaceAll('~1', '/').replaceAll('~0', '~')
}

/** Every component schema a `$ref` under `node` names, and the ones they name in turn. */
export function reached(schemas, node, seen = new Set()) {
  if (Array.isArray(node)) {
    for (const v of node) reached(schemas, v, seen)
  } else if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) {
      if (k === '$ref' && typeof v === 'string' && v.startsWith(SCHEMAS)) {
        const name = unescapePointer(v.slice(SCHEMAS.length))
        if (!seen.has(name)) {
          seen.add(name)
          reached(schemas, schemas[name], seen)
        }
      } else {
        reached(schemas, v, seen)
      }
    }
  }
  return seen
}

/** openapi-typescript's `transformProperty` for the spec `doc`, built with `ts`. */
export function requestDefaultsHook(doc, ts) {
  const schemas = doc.components?.schemas ?? {}
  const operations = Object.values(doc.paths ?? {}).flatMap((item) => Object.values(item))
  const requests = reached(schemas, operations.map((op) => op?.requestBody))
  const responses = reached(schemas, operations.map((op) => op?.responses))
  return (property, schema, { path }) => {
    if (!path?.startsWith(SCHEMAS) || property.questionToken || !('default' in schema)) return undefined
    const [name, key, ...deeper] = path.slice(SCHEMAS.length).split('/').map(unescapePointer)
    if (deeper.length || !requests.has(name) || responses.has(name)) return undefined
    if (schemas[name].required?.includes(key)) return undefined
    return ts.factory.updatePropertySignature(
      property,
      property.modifiers,
      property.name,
      ts.factory.createToken(ts.SyntaxKind.QuestionToken),
      property.type,
    )
  }
}
