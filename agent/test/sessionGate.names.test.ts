import { readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { ALL_TOOLS } from '../src/tools/index.js'
import { SESSION_GATE_ACTIVITIES } from '../src/temporal/session/names.js'

// Temporal routes an activity by queue plus name, so a name both halves register would let
// one half's worker be sent the other's payload shape. The TS side (every activity the
// agent's workers register, and the session gate's) shares no name with agent-durable's,
// read from its source so the check stays true as either side changes.

const here = path.dirname(fileURLToPath(import.meta.url))
const walk = (dir: string): string[] =>
  readdirSync(dir).flatMap((f) => {
    const full = path.join(dir, f)
    return statSync(full).isDirectory() ? (f === '__pycache__' ? [] : walk(full)) : [full]
  })

/** The literal names agent-durable gives its activities: `@activity.defn(name="…")`. */
const pythonNames = walk(path.join(here, '../../agent-durable/src'))
  .filter((f) => f.endsWith('.py'))
  .flatMap((f) => [...readFileSync(f, 'utf8').matchAll(/activity\.defn\(\s*name\s*=\s*"([^"]+)"/g)].map((m) => m[1]!))

/** Every activity name the agent's workers register by a constant in src/temporal, plus its tools. */
const tsNames = [
  ...walk(path.join(here, '../src/temporal'))
    .filter((f) => f.endsWith('.ts'))
    .flatMap((f) => [...readFileSync(f, 'utf8').matchAll(/export const \w*ACTIVITY\w* = '([^']+)'/g)].map((m) => m[1]!)),
  ...Object.values(SESSION_GATE_ACTIVITIES),
]

describe('activity names', () => {
  it('reads the names it compares', () => {
    expect(pythonNames).toEqual(expect.arrayContaining(['open_input', 'resolve_input', 'grant_input', 'deny_input', 'gate_settings', 'finish_turn', 'follow_session']))
    expect(tsNames).toEqual(expect.arrayContaining(['gate.open_input', 'gate.describe_call', 'turn_tool_tier', 'memory_recall']))
  })

  it("shares none between the agent's workers and agent-durable's", () => {
    expect(tsNames.filter((n) => pythonNames.includes(n))).toEqual([])
  })

  it('keeps the gate off every tool name too, which agent-durable stubs and the agent serves', () => {
    const tools = new Set(ALL_TOOLS.map((t) => t.name))
    expect(Object.values(SESSION_GATE_ACTIVITIES).filter((n) => tools.has(n) || pythonNames.includes(n))).toEqual([])
  })
})
