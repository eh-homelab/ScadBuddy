// Writes src/api/schema.d.ts from the backend's OpenAPI spec, and
// src/storefront/api/schema.d.ts from the storefront edge's (#5282). It also writes
// src/api/settingDocs.json, every setting's label and help (#887), from the default of
// the spec's `SettingsView.docs`: the mocks serve it, so they never restate the
// backend's text. None of these files is committed (#492), so typecheck, test and build
// run this first.
//
// Each spec comes from its variable when it is set, `SCADBUDDY_OPENAPI_JSON` and
// `SCADBUDDY_STOREFRONT_OPENAPI_JSON` (the Dockerfile's `api-spec` stage exports both
// and this stage copies them in, with no Python here). Otherwise it is exported now
// from ../backend, which needs uv.
import { execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
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

const docs = JSON.parse(readFileSync(targets[0][0], 'utf8')).components.schemas.SettingsView.properties.docs.default
if (!docs || typeof docs !== 'object') throw new Error('the spec has no SettingsView.docs default (#887)')
writeFileSync(`${pkg}/src/api/settingDocs.json`, `${JSON.stringify(docs, null, 2)}\n`)
