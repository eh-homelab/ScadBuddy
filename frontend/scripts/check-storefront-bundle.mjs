// Checks a production build (`pnpm build`, no VITE_MOCK_API) for the public storefront
// page's rule (spec 2026-11-02 §5.1, #5282): the `storefront.html` entry loads nothing of
// the back office. The storefront edge serves exactly this closure
// (backend/scadbuddy/storefront/static.py `_closure`), so it is read the same way from
// dist/.vite/manifest.json: each chunk's `file`, `css`, `assets`, `imports` and
// `dynamicImports`, recursively.
//
// Refused in the closure:
// - a chunk whose source is under src/mocks/ (the msw mocks, a mocked build only);
// - a script naming a back-office API path (`/api/v1/`): the back office's `api/client`
//   and anything else that calls it. The page's own client calls `/api/store/v1` only.
//
// Usage: node scripts/check-storefront-bundle.mjs [dist]   (exits 1 and names each fault)
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import process from 'node:process'

const ENTRY = 'storefront.html'
const BACK_OFFICE_API = '/api/v1/'

export function closure(manifest, key = ENTRY) {
  const seen = new Set()
  const pending = [key]
  while (pending.length) {
    const current = pending.pop()
    if (seen.has(current) || !(current in manifest)) continue
    seen.add(current)
    pending.push(...(manifest[current].imports ?? []), ...(manifest[current].dynamicImports ?? []))
  }
  return [...seen]
}

export function faults(dist) {
  const manifest = JSON.parse(readFileSync(join(dist, '.vite', 'manifest.json'), 'utf8'))
  if (!(ENTRY in manifest)) return [`${ENTRY} is not an entry of this build`]
  const found = []
  for (const key of closure(manifest)) {
    const chunk = manifest[key]
    const source = chunk.src ?? key
    if (source.startsWith('src/mocks/')) found.push(`${source}: a mock is in the storefront bundle`)
    if (chunk.file?.endsWith('.js') && readFileSync(join(dist, chunk.file), 'utf8').includes(BACK_OFFICE_API)) {
      found.push(`${chunk.file} (${source}): names the back office API (${BACK_OFFICE_API})`)
    }
  }
  return found
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const dist = process.argv[2] ?? 'dist'
  const found = faults(dist)
  for (const fault of found) process.stderr.write(`storefront bundle: ${fault}\n`)
  if (found.length) process.exit(1)
  process.stdout.write('storefront bundle: no back-office API and no mocks in the storefront.html closure\n')
}
