import { describe, expect, it } from 'vitest'
import type { FlowHostInterface } from '../api/types'
import { flowCompletions, flowHover, problemMarkers } from './flowLanguage'

const HELP: FlowHostInterface = {
  functions: [
    {
      name: 'render',
      signature: 'async def render(slug: str, params: dict[str, Any]) -> RenderResult',
      parameters: ['slug', 'params'],
      doc: 'Render template `slug` with `params`.',
    },
    { name: 'sleep', signature: 'async def sleep(seconds: float) -> None', parameters: ['seconds'], doc: 'Wait.' },
  ],
  types: [{ name: 'RenderResult', source: 'class RenderResult(TypedDict):\n    job_id: str' }],
  tools: [
    { name: 'list_models', tier: 'read' },
    { name: 'send_to_bambuddy', tier: 'outward' },
  ],
}

describe('problemMarkers', () => {
  it("spans each problem's line, and puts a problem with no line on line 1", () => {
    const source = 'import asyncio\nasync def main():\n    await sleep("x")\n'
    const markers = problemMarkers(
      [
        { line: 3, message: 'error[invalid-argument-type]' },
        { line: null, message: 'script is over 64 KiB' },
      ],
      source,
    )
    expect(markers.map((m) => [m.startLineNumber, m.startColumn, m.endColumn, m.severity])).toEqual([
      [3, 5, 21, 8],
      [1, 1, 15, 8],
    ])
  })
})

describe('flowCompletions', () => {
  it('offers the host functions as snippets with a stop per parameter, and the result types', () => {
    const items = flowCompletions('    job = await ren', HELP)
    expect(items.map((i) => i.label)).toEqual(['render', 'sleep', 'RenderResult'])
    expect(items[0]).toMatchObject({
      kind: 'function',
      detail: 'async def render(slug: str, params: dict[str, Any]) -> RenderResult',
      insertText: 'render(${1:slug}, ${2:params})',
      snippet: true,
    })
  })

  it("offers the manifest's tool names inside tool(\"…\") and tool_result('…')", () => {
    for (const before of ['    await tool("li', "    r = await tool_result('", '    await tool( "']) {
      const items = flowCompletions(before, HELP)
      expect(items.map((i) => [i.label, i.detail]), before).toEqual([
        ['list_models', 'read tool'],
        ['send_to_bambuddy', 'outward tool'],
      ])
    }
  })

  it('offers nothing in another string or a comment', () => {
    expect(flowCompletions('    print("ren', HELP)).toEqual([])
    expect(flowCompletions('    # ren', HELP)).toEqual([])
    expect(flowCompletions('    x = "a" + ren', HELP).length).toBeGreaterThan(0)
  })
})

describe('flowHover', () => {
  it("shows a host function's signature and doc, a type's fields, and a tool's tier", () => {
    expect(flowHover('render', 'await render("box", {})', HELP)).toEqual([
      '```python\nasync def render(slug: str, params: dict[str, Any]) -> RenderResult\n```',
      'Render template `slug` with `params`.',
    ])
    expect(flowHover('RenderResult', 'x: RenderResult', HELP)?.[0]).toContain('job_id: str')
    expect(flowHover('send_to_bambuddy', 'await tool("send_to_bambuddy", {})', HELP)).toEqual([
      "ScadBuddy's tool `send_to_bambuddy` (outward). It waits for a person to approve each call.",
    ])
    expect(flowHover('list_models', 'list_models = 1', HELP)).toBeNull()
    expect(flowHover('main', 'async def main():', HELP)).toBeNull()
  })
})
