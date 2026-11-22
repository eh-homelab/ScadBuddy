import { readdir, readFile } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// #5299 step 12: the agent holds no database. Its data is behind the API's CRUD routes, and it
// has no SQL client at all: no `src` file loads `postgres` (or any other Postgres driver), the
// package is a test dependency only, and `src/db.ts` is gone. The tests may still seed and read
// the `ai_*` tables over SQL (test/support/database.ts); `src` may not. The older guard stays:
// no SQL in `src` names a backend table whose grants the API took back (backend auth/revoke.py
// `RETIRED_ROLES` retires the agent's whole role now). `evals/` is exempt on purpose: an
// operator's live eval run (evals/credential.ts) opens the saved credential from Postgres with
// the API's KEK, outside the service; never copy that pattern into `src`.
const RETIRED = ['events', 'workflow_runs', 'workflow_run_decisions', 'flow_grants', 'flow_grant_uses']
const SRC = fileURLToPath(new URL('../src', import.meta.url))
// `\s+` spans line breaks, so a table name on the line after its keyword is found too.
const SQL_NAMING = new RegExp(String.raw`\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:${RETIRED.join('|')})\b`, 'gi')

/**
 * Where `text`'s SQL names a retired table, as `line: text` of the keyword's line. Comment lines
 * are blanked first, keeping the line numbers: a line that starts with `//`, and every line of a
 * block comment that starts a line (`/*` or `/**` up to its `*\/`, keeping the code after it). A line that only starts with
 * `*` is code, such as a select list's `* FROM`.
 */
function naming(text: string): string[] {
  const lines = text.split('\n')
  let inBlock = false
  const code = lines
    .map((line) => {
      const t = line.trim()
      if (inBlock) {
        const end = t.indexOf('*/')
        inBlock = end < 0
        // The closing line keeps the code after the `*\/`.
        return end < 0 ? '' : t.slice(end + 2)
      }
      if (t.startsWith('//')) return ''
      if (t.startsWith('/*')) {
        const end = t.indexOf('*/', 2)
        inBlock = end < 0
        // A block that ends on its own line keeps the code after it.
        return end < 0 ? '' : t.slice(end + 2)
      }
      return line
    })
    .join('\n')
  return [...code.matchAll(SQL_NAMING)].map((match) => {
    const line = code.slice(0, match.index).split('\n').length
    return `${line}: ${lines[line - 1]!.trim()}`
  })
}

async function sources(dir: string): Promise<string[]> {
  const found: string[] = []
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) {
      // The agent's own migrations never name a backend table; the generated client is no SQL.
      if (entry.name !== 'migrations') found.push(...(await sources(path)))
    } else if (entry.name.endsWith('.ts') && !entry.name.endsWith('.d.ts')) {
      found.push(path)
    }
  }
  return found
}

describe('the backend tables whose grants the API took back', () => {
  it('are found where SQL names them, on one line or across lines', () => {
    expect(naming('const [row] = await sql`SELECT max(seq) FROM events`')).toHaveLength(1)
    expect(naming('await sql`\n  SELECT steps\n    FROM\n      workflow_runs r WHERE id = ${id}`')).toEqual(['3: FROM'])
    expect(naming('UPDATE\nworkflow_run_decisions SET x = 1')).toEqual(['1: UPDATE'])
    // A select list's `*` that starts a line is SQL, not a doc comment.
    expect(naming('await sql`SELECT\n  * FROM events`')).toEqual(['2: * FROM events`'])
    expect(naming('/**\n * Reads\n * FROM events, in a doc comment.\n */\nconst x = 1')).toEqual([])
    expect(naming('/* FROM events */ const y = 1')).toEqual([])
    expect(naming('/* a note */ const y = await sql`SELECT 1 FROM workflow_runs`')).toHaveLength(1)
    expect(naming('/* note\n*/ const r = await sql`SELECT * FROM events`')).toEqual(['2: */ const r = await sql`SELECT * FROM events`'])
    // Prose and comments name them too, and are not SQL.
    expect(naming('// reads FROM events through the routes\n/**\n * JOIN workflow_runs in a doc comment\n */')).toEqual([])
    expect(naming('the events log, read from the workflow_runs projection')).toEqual([])
    expect(naming('SELECT 1 FROM events_archive JOIN flow_grants_old g ON true')).toEqual([])
    expect(naming('SELECT 1 FROM flow_grants g JOIN flow_grant_uses u ON u.grant_id = g.id')).toHaveLength(2)
  })

  it('are named by no SQL in the agent', async () => {
    const found: string[] = []
    for (const file of await sources(SRC)) {
      for (const hit of naming(await readFile(file, 'utf8'))) found.push(`${relative(SRC, file)}:${hit}`)
    }
    expect(found).toEqual([])
  })
})

/** The Postgres drivers a Node process could load. */
const DRIVERS = ['postgres', 'pg', 'pg-native', '@neondatabase/serverless', 'slonik', 'kysely', 'knex']
// `import … from 'x'`, `import 'x'`, `import('x')`, `require('x')`, `export … from 'x'`, a type import too.
const LOADS = new RegExp(String.raw`(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"](${DRIVERS.map((d) => d.replace(/[/.@-]/g, '\\$&')).join('|')})['"]`, 'g')

function loads(text: string): string[] {
  return [...text.matchAll(LOADS)].map((m) => m[1]!)
}

describe('the agent has no SQL client (#5299 step 12)', () => {
  it('finds a driver however it is loaded', () => {
    expect(loads("import postgres, { type Sql } from 'postgres'")).toEqual(['postgres'])
    expect(loads("import type { Sql } from 'postgres'")).toEqual(['postgres'])
    expect(loads("const { Client } = require('pg')")).toEqual(['pg'])
    expect(loads("const pg = await import('pg')")).toEqual(['pg'])
    expect(loads("export { default } from 'knex'")).toEqual(['knex'])
    expect(loads("import { x } from './postgres.js'\nimport y from 'postgresql-client-docs'")).toEqual([])
  })

  it('loads no Postgres driver anywhere in src', async () => {
    const found: string[] = []
    for (const file of await sources(SRC)) {
      for (const driver of loads(await readFile(file, 'utf8'))) found.push(`${relative(SRC, file)}: ${driver}`)
    }
    expect(found).toEqual([])
  })

  it('ships none: a driver is at most a test dependency', async () => {
    const pkg = JSON.parse(await readFile(fileURLToPath(new URL('../package.json', import.meta.url)), 'utf8')) as {
      dependencies?: Record<string, string>
    }
    expect(Object.keys(pkg.dependencies ?? {}).filter((name) => DRIVERS.includes(name))).toEqual([])
  })

  it('has no database module', () => {
    expect(existsSync(join(SRC, 'db.ts'))).toBe(false)
  })
})
