import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { describe, expect, it } from 'vitest'
import { callContext } from '../src/harness/callContext.js'
import { TOOL_USE_META } from '../src/telemetry/trace.js'

// #1222: an in-process tool call's context (its tool_use id under Claude Code's `_meta`
// key, and its abort signal) is parsed once, in src/harness/callContext.ts, and the key
// is declared once, in src/telemetry/trace.ts. A handler with its own copy could drift
// from the measured key.

const SRC = join(import.meta.dirname, '..', 'src')

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) return name === 'migrations' ? [] : sources(path)
    return path.endsWith('.ts') ? [path] : []
  })
}

describe('callContext', () => {
  const signal = new AbortController().signal

  it("reads the tool_use id from Claude Code's _meta key, with the call's signal", () => {
    expect(callContext({ signal, _meta: { [TOOL_USE_META]: 'toolu_1' } })).toEqual({ ok: true, toolUseId: 'toolu_1', signal })
  })

  it('says why when the id or the signal is missing', () => {
    expect(callContext({ signal })).toEqual({ ok: false, error: 'the call has no tool_use id' })
    expect(callContext({ _meta: { [TOOL_USE_META]: 'toolu_1' } })).toMatchObject({
      ok: false,
      error: expect.stringMatching(/^the call's context is not as expected/),
    })
  })

  it("is the only place the _meta key is spelled, beside its declaration", () => {
    const copies = sources(SRC)
      .filter((path) => !path.endsWith(join('telemetry', 'trace.ts')))
      .filter((path) => readFileSync(path, 'utf8').includes(TOOL_USE_META))
      .map((path) => relative(SRC, path))
    expect(copies).toEqual([])
  })
})
