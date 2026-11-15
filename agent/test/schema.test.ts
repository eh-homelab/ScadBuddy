import { execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { EXPECTED_MIGRATIONS } from '../src/db/schema.js'
import {
  DEFAULT_LOCK_TIMEOUT_MS,
  DEFAULT_STATEMENT_TIMEOUT_MS,
  LEGACY_VERSIONS,
  loadMigrations,
  MIGRATION_LOCK,
  migrationChecksum,
} from './support/migrations.js'

// #5299 D6: the API applies the agent's migrations (backend/scadbuddy/render/agent_migrations.py),
// the agent waits for the files it lists (src/db/expected-migrations.json), and the tests
// migrate with the TypeScript runner (support/migrations.ts). These three must never drift.

const EXPECTED_FILE = new URL('../src/db/expected-migrations.json', import.meta.url)
const BACKEND = fileURLToPath(new URL('../../backend/', import.meta.url))

const files = loadMigrations().map((m) => ({ id: m.id, sha256: migrationChecksum(m) }))

// `UPDATE_EXPECTED_MIGRATIONS=1 pnpm exec vitest run test/schema.test.ts` after adding a file.
if (process.env.UPDATE_EXPECTED_MIGRATIONS) writeFileSync(EXPECTED_FILE, `${JSON.stringify(files, null, 2)}\n`)

/** What the API's runner reads, as JSON (the agent job has uv and the backend tree, as `gen:api` needs). */
const PYTHON = `
import json
from scadbuddy.render import agent_migrations as m
print(json.dumps({
    "files": [{"id": f.id, "sha256": m.agent_migration_checksum(f)} for f in m.AGENT_MIGRATIONS],
    "lock": str(m.AGENT_MIGRATION_LOCK),
    "lock_timeout_ms": m.DEFAULT_LOCK_TIMEOUT_MS,
    "statement_timeout_ms": m.DEFAULT_STATEMENT_TIMEOUT_MS,
    "legacy": [{"id": i, "story": s} for i, s in m.AGENT_LEGACY_VERSIONS],
}))
`

describe("the agent's migrations (#5299)", () => {
  it('the agent expects every file of backend/scadbuddy/migrations_agent/, with its checksum', () => {
    expect(JSON.parse(readFileSync(EXPECTED_FILE, 'utf8'))).toEqual(files)
    expect(EXPECTED_MIGRATIONS).toEqual(files)
  })

  it("the API's runner reads the same files, with the same checksums, lock, timeouts and legacy ids", { timeout: 180_000 }, () => {
    const out = execFileSync('uv', ['run', '--frozen', 'python', '-c', PYTHON], {
      cwd: BACKEND,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'inherit'],
    })
    expect(JSON.parse(out)).toEqual({
      files,
      lock: MIGRATION_LOCK.toString(),
      lock_timeout_ms: DEFAULT_LOCK_TIMEOUT_MS,
      statement_timeout_ms: DEFAULT_STATEMENT_TIMEOUT_MS,
      legacy: LEGACY_VERSIONS,
    })
  })
})
