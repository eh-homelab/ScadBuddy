import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'

// #1383: "is this ai_questions row waiting on the user?" is spelled once, in
// src/questions/waiting.ts. A query that writes its own copy of the done rule could
// drift from it (the old `outcome IS NULL` check alone counted a posted done summary as
// a wait); its behaviour is checked by test/attention.pg.test.ts.

const SRC = join(import.meta.dirname, '..', 'src')

/** The rule's two halves as a query would spell them: "not a done summary", and "a done summary" (done, no timer). */
const RULE = /attention_reason\s*(IS DISTINCT FROM|<>)\s*'done'|attention_reason\s*(=|IS NOT DISTINCT FROM)\s*'done'\s+AND\s+(\w+\.)?expires_at\s+IS\s+NULL/

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return name === 'migrations' ? [] : sources(path)
    return path.endsWith('.ts') ? [path] : []
  })
}

describe('the done-summary rule', () => {
  it('is written only in questions/waiting.ts', () => {
    const copies = sources(SRC)
      .filter((path) => !path.endsWith(join('questions', 'waiting.ts')))
      .filter((path) => RULE.test(readFileSync(path, 'utf8')))
      .map((path) => relative(SRC, path))
    expect(copies).toEqual([])
  })
})
