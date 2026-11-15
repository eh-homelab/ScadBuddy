import { readdir, readFile } from 'node:fs/promises'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// #5299: the API takes back the agent's grants on these backend tables at every start
// (backend auth/revoke.py `RETIRED_GRANTS`). The agent reads them only through the backend's
// CRUD routes (flows/backendReads.ts, events/eventLogReads.ts), so no SQL of its own names one.
const RETIRED = ['events', 'workflow_runs', 'workflow_run_decisions']
const SRC = fileURLToPath(new URL('../src', import.meta.url))
const SQL_NAMING = new RegExp(String.raw`\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:${RETIRED.join('|')})\b`, 'i')

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
  it('are named by no SQL in the agent', async () => {
    const naming: string[] = []
    for (const file of await sources(SRC)) {
      const lines = (await readFile(file, 'utf8')).split('\n')
      lines.forEach((line, i) => {
        if (!line.trim().startsWith('//') && !line.trim().startsWith('*') && SQL_NAMING.test(line)) {
          naming.push(`${relative(SRC, file)}:${i + 1}: ${line.trim()}`)
        }
      })
    }
    expect(naming).toEqual([])
  })
})
