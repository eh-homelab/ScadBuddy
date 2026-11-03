import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { TOOL_SEARCH_MODES, UPFRONT_TOOLS } from '../src/harness/toolSearch.js'
import { SYSTEM_PROMPT_APPEND_MAX, TURN_DEFAULTS } from '../src/sessions/turnSettings.js'
import { ALL_TOOLS } from '../src/tools/index.js'

// The Settings section's copies of the turn settings' values (frontend/src/api/sessionTurn.ts,
// #5221, #5222), loaded as they are, as test/support/frontendProtocol.ts loads the panel's
// protocol. The path is computed so tsc does not pull the frontend package into the program.

const here = path.dirname(fileURLToPath(import.meta.url))
const modulePath = path.resolve(here, '../../frontend/src/api/sessionTurn.ts')

type Copy = { TOOL_SEARCH_MODES: readonly string[]; SYSTEM_PROMPT_BASES: readonly string[]; SYSTEM_PROMPT_APPEND_MAX: number }

describe('the turn settings', () => {
  it("are the frontend's copies", async () => {
    const copy = (await import(modulePath)) as Copy
    expect(copy.TOOL_SEARCH_MODES).toEqual(TOOL_SEARCH_MODES)
    expect(copy.SYSTEM_PROMPT_BASES).toEqual(['claude_code', 'minimal'])
    expect(copy.SYSTEM_PROMPT_BASES).toContain(TURN_DEFAULTS.system_prompt_base)
    expect(copy.SYSTEM_PROMPT_APPEND_MAX).toBe(SYSTEM_PROMPT_APPEND_MAX)
  })

  it('upfront, name only tools the registry has', () => {
    const names = new Set(ALL_TOOLS.map((t) => t.name))
    expect([...UPFRONT_TOOLS].filter((n) => !names.has(n))).toEqual([])
  })
})
