// Writes src/api/schema.d.ts from the backend's OpenAPI spec. Neither file is
// committed (#492), so typecheck, test and build run this first.
//
// The spec comes from `SCADBUDDY_OPENAPI_JSON` when it is set (the Dockerfile's
// `api-spec` stage exports it and this stage copies it in, with no Python
// here). Otherwise it is exported now from ../backend, which needs uv.
import { execFileSync } from 'node:child_process'
import process from 'node:process'
import { URL, fileURLToPath } from 'node:url'

const pkg = fileURLToPath(new URL('..', import.meta.url))
const backend = fileURLToPath(new URL('../../backend', import.meta.url))
let spec = process.env.SCADBUDDY_OPENAPI_JSON
if (!spec) {
  spec = `${backend}/openapi.json`
  execFileSync('uv', ['run', '--frozen', 'python', '-m', 'scadbuddy.tools.export_openapi', spec], {
    cwd: backend,
    stdio: ['ignore', 'ignore', 'inherit'],
  })
}
execFileSync('pnpm', ['exec', 'openapi-typescript', spec, '-o', 'src/api/schema.d.ts'], {
  cwd: pkg,
  stdio: 'inherit',
})
