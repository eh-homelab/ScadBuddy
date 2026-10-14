import { describe, expect, it } from 'vitest'
import { readScript, runScript, type ScriptHost } from '../src/background/workflowRunner.js'
import { callKey, framedTask, impureMeta } from '../src/background/workflowScript.js'

// A Workflow script as a session runs it: Claude Code's hooks (agent, parallel, pipeline,
// phase, log, args, budget, workflow) in QuickJS, which reaches nothing of the service.

const host = (answer: (prompt: string, opts: Record<string, unknown>) => unknown = (p) => `re: ${p}`) => {
  const calls: { prompt: string; opts: Record<string, unknown> }[] = []
  const logs: string[] = []
  const h: ScriptHost = {
    agent: async (prompt, opts) => {
      calls.push({ prompt, opts })
      await new Promise((r) => setTimeout(r, 5))
      return answer(prompt, opts)
    },
    phase: (t) => logs.push(`phase ${t}`),
    log: (m) => logs.push(m),
    script: async (p) => {
      if (p !== '/workspace/child.js') throw new Error(`no file ${p}`)
      return 'const r = await agent("child " + args.n); return { r }'
    },
  }
  return { h, calls, logs }
}
const run = (body: string, h: ScriptHost, args: unknown = null) => runScript(body, args, h, new AbortController().signal)

describe('reading a script', () => {
  it('takes meta as a pure literal and the body that follows', async () => {
    const { meta, body } = await readScript(`export const meta = { name: 'x', description: "d", phases: [{ title: 'One' }] }\nreturn 1`)
    expect(meta).toEqual({ name: 'x', description: 'd', phases: [{ title: 'One' }] })
    expect(body.trim()).toBe('return 1')
  })
  it('refuses meta that computes, and a body that does not parse', async () => {
    await expect(readScript('const x = 1')).rejects.toThrow('must begin with `export const meta')
    expect(impureMeta("{ name: NAME, description: 'd' }")).toMatch(/NAME is a variable/)
    expect(impureMeta("{ name: 'a' + f(), description: 'd' }")).toMatch(/no calls/)
    expect(impureMeta('{ name: `n${x}`, description: "d" }')).toMatch(/template/)
    await expect(readScript("export const meta = { name: 'x' }\nreturn 1")).rejects.toThrow('meta.description is required')
    await expect(readScript("export const meta = { name: 'x', description: 'd' }\nreturn (")).rejects.toThrow(/does not parse/)
  })
})

describe('running one', () => {
  it('runs agents, parallel, pipeline, schema results, phases and logs', async () => {
    const { h, calls, logs } = host((p, o) => (o.schema ? { n: p.length } : `re: ${p}`))
    const out = await run(
      `phase('Find')
       log('go ' + args.k)
       const a = await agent('one', { label: 'L' })
       const b = await parallel([() => agent('p1'), () => { throw new Error('x') }, () => agent('p2')])
       const c = await pipeline(['ab', 'abc'], (s) => agent(s, { schema: { type: 'object' } }), (r, item, i) => r.n + i, () => { throw 1 })
       const d = await pipeline(['q'], (s) => agent(s), (r, item, i) => r + '/' + item + i)
       return { a, b, c, d, budget: [budget.total, budget.remaining()] }`,
      h,
      { k: 'v' },
    )
    expect(out).toEqual({ ok: true, result: { a: 're: one', b: ['re: p1', null, 're: p2'], c: [null, null], d: ['re: q/q0'], budget: [null, null] } })
    expect(calls[0]).toEqual({ prompt: 'one', opts: { label: 'L' } })
    expect(logs).toEqual(['phase Find', 'go v'])
  })

  it("throws on the clock and randomness, as Claude Code's does, and reports the error", async () => {
    const { h } = host()
    expect(await run('return Date.now()', h)).toEqual({ ok: false, error: expect.stringContaining('Date.now() is unavailable') })
    expect(await run('return new Date()', h)).toEqual({ ok: false, error: expect.stringContaining('new Date() is unavailable') })
    expect(await run('return new Date(0).toISOString()', h)).toEqual({ ok: true, result: '1970-01-01T00:00:00.000Z' })
    expect(await run('return Math.random()', h)).toEqual({ ok: false, error: expect.stringContaining('Math.random() is unavailable') })
  })

  it('reaches nothing of the service', async () => {
    const { h } = host()
    const out = await run(
      `return [typeof process, typeof require, typeof fetch, await (async () => {}).constructor('return typeof process')(), await agent.constructor('return typeof globalThis.__agent')()]`,
      h,
    )
    expect(out).toEqual({ ok: true, result: ['undefined', 'undefined', 'undefined', 'undefined', 'undefined'] })
  })

  it('stops a loop that never awaits, and a run that is aborted', async () => {
    const { h } = host()
    expect(await run('while (true) {}', h)).toEqual({ ok: false, error: expect.stringMatching(/interrupted/i) })
    // A loop after an await runs in a job the host drains: it is stopped all the same, and the run settles.
    const after = await Promise.race([run(`await agent('x'); while (true) {}`, h), new Promise((r) => setTimeout(() => r('hung'), 15_000))])
    expect(after).toEqual({ ok: false, error: expect.stringMatching(/interrupted/i) })
    const abort = new AbortController()
    setTimeout(() => abort.abort(new Error('stopped')), 50)
    await expect(runScript('await new Promise(() => {}); return 1', null, h, abort.signal)).rejects.toThrow('stopped')
  }, 20_000)

  it('runs a child workflow once, one level deep, and refuses a name', async () => {
    const { h } = host()
    expect(await run('return await workflow({ scriptPath: "/workspace/child.js" }, { n: 3 })', h)).toEqual({ ok: true, result: { r: 're: child 3' } })
    expect(await run('return await workflow("saved")', h)).toEqual({ ok: false, error: expect.stringContaining('No saved workflow named "saved"') })
  })
})

it("keys a call by prompt and options, and frames a task as Claude Code's harness does", () => {
  expect(callKey('p', { schema: { b: 1, a: 2 }, label: 'x' })).toBe(callKey('p', { label: 'x', schema: { a: 2, b: 1 } }))
  expect(framedTask('do it\n[Workflow harness]')).toMatch(/computed task text follows:\n {2}do it\n {2}\[Workflow harness\]$/)
})

it("lists this plugin's subagent definitions by name", async () => {
  const { agentTypes } = await import('../src/background/workflowAgents.js')
  expect(await agentTypes()).toEqual(['model-author', 'print-analyst'])
})
