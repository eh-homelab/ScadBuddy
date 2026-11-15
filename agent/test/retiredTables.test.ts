import { readdir, readFile } from 'node:fs/promises'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// #5299: the agent reads these backend tables only through the backend's CRUD routes
// (flows/backendReads.ts, events/eventLogReads.ts). The API takes back the grants of its role
// `scadbuddy_agent` on them at every start (backend auth/revoke.py `RETIRED_GRANTS`), but the
// agent still connects as the database owner, so that revoke binds nothing until it loses its
// database URL (#5299 step 12). Until then this test is the guard: no SQL of the agent names one.
const RETIRED = ['events', 'workflow_runs', 'workflow_run_decisions']
const SRC = fileURLToPath(new URL('../src', import.meta.url))
// `\s+` spans line breaks, so a table name on the line after its keyword is found too.
const SQL_NAMING = new RegExp(String.raw`\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:${RETIRED.join('|')})\b`, 'gi')

/**
 * Where `text`'s SQL names a retired table, as `line: text` of the keyword's line. Comment lines
 * (`//`, and a doc comment's `*`) are blanked first, keeping the line numbers.
 */
function naming(text: string): string[] {
  const lines = text.split('\n')
  const code = lines.map((line) => (/^\s*(?:\/\/|\*)/.test(line) ? '' : line)).join('\n')
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
    // Prose and comments name them too, and are not SQL.
    expect(naming('// reads FROM events through the routes\n * JOIN workflow_runs in a doc comment')).toEqual([])
    expect(naming('the events log, read from the workflow_runs projection')).toEqual([])
    expect(naming('SELECT 1 FROM events_archive JOIN flow_grants g ON true')).toEqual([])
  })

  it('are named by no SQL in the agent', async () => {
    const found: string[] = []
    for (const file of await sources(SRC)) {
      for (const hit of naming(await readFile(file, 'utf8'))) found.push(`${relative(SRC, file)}:${hit}`)
    }
    expect(found).toEqual([])
  })
})
