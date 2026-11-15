// Writes src/api/schema.d.ts from the backend's OpenAPI spec, and
// src/storefront/api/schema.d.ts from the storefront edge's (#5282). None of these
// files is committed (#492), so typecheck, test and build run this first.
//
// Each spec comes from its variable when it is set, `SCADBUDDY_OPENAPI_JSON` and
// `SCADBUDDY_STOREFRONT_OPENAPI_JSON` (the Dockerfile's `api-spec` stage exports both
// and this stage copies them in, with no Python here). Otherwise it is exported now
// from ../backend, which needs uv.
import { execFileSync } from 'node:child_process'
import process from 'node:process'
import { URL, fileURLToPath } from 'node:url'

const pkg = fileURLToPath(new URL('..', import.meta.url))
const backend = fileURLToPath(new URL('../../backend', import.meta.url))

function exported(variable, file, flags) {
  const given = process.env[variable]
  if (given) return given
  const spec = `${backend}/${file}`
  execFileSync('uv', ['run', '--frozen', 'python', '-m', 'scadbuddy.tools.export_openapi', ...flags, spec], {
    cwd: backend,
    stdio: ['ignore', 'ignore', 'inherit'],
  })
  return spec
}

const targets = [
  [exported('SCADBUDDY_OPENAPI_JSON', 'openapi.json', []), 'src/api/schema.d.ts'],
  [exported('SCADBUDDY_STOREFRONT_OPENAPI_JSON', 'storefront-openapi.json', ['--storefront']), 'src/storefront/api/schema.d.ts'],
]
for (const [spec, out] of targets) {
  execFileSync('pnpm', ['exec', 'openapi-typescript', spec, '-o', out], { cwd: pkg, stdio: 'inherit' })
}
