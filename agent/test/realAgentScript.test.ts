import { describe, expect, it } from 'vitest'
import { BLANK_TEMPLATE } from '../src/tools/templates.js'
import type { MessagesBody } from './support/fakeAnthropic.js'
import { DONE, HELLO, KEYCHAIN, NOT_DONE, SCENARIOS, scriptedReply, TOOL_PREFIX } from './support/realAgentScript.js'

// The script frontend/e2e/real-agent.spec.ts drives the real agent with (#1923).

const tools = Object.values(SCENARIOS)
  .flat()
  .map((step) => ({ name: `${TOOL_PREFIX}${step.tool}` }))

const prompt = (text: string) => ({ role: 'user', content: [{ type: 'text', text: `<page_context>/</page_context>\n${text}` }] })
const call = (name: string) => ({ role: 'assistant', content: [{ type: 'tool_use', id: 't', name, input: {} }] })
const result = (text: string, isError = false) => ({
  role: 'user',
  content: [
    { type: 'tool_result', tool_use_id: 't', content: [{ type: 'text', text }], is_error: isError },
    { type: 'text', text: '<system-reminder>context Claude Code appends</system-reminder>' },
  ],
})
const body = (...messages: { role: string; content: unknown }[]): MessagesBody => ({ tools, messages })

describe('the real-agent script', () => {
  it('says hello without a marker, and Done. once a tool has answered', () => {
    expect(scriptedReply(body(prompt('Say hello')))).toEqual({ text: HELLO })
    expect(scriptedReply(body(prompt('Say hello'), call('x'), result('{}')))).toEqual({ text: DONE })
  })

  it('calls the outward tool, and reports a denial as not done', () => {
    expect(scriptedReply(body(prompt('Remember my print options [outward]')))).toEqual({
      toolUse: { name: `${TOOL_PREFIX}set_print_options`, input: { scope: 'global', options: {} } },
    })
    const denied = scriptedReply(body(prompt('[outward]'), call('x'), result('The user denied this tool call.', true)))
    expect(denied).toEqual({ text: `${NOT_DONE}The user denied this tool call.` })
    expect(scriptedReply(body(prompt('[outward]'), call('x'), result('{"saved": true}')))).toEqual({ text: DONE })
  })

  it('answers a request that offers no ScadBuddy tools with text, marker or not', () => {
    expect(scriptedReply({ messages: [prompt('[outward]')] })).toEqual({ text: HELLO })
  })

  it('renders the keychain, and holds a render that did not finish as not done', () => {
    expect(scriptedReply(body(prompt('[keychain]')))).toEqual({
      toolUse: { name: `${TOOL_PREFIX}render_model`, input: { slug: KEYCHAIN, params: { name: 'Ada', text_color: '#FF0000' } } },
    })
    expect(scriptedReply(body(prompt('[keychain]'), call('x'), result('{\n  "status": "done"\n}')))).toEqual({ text: DONE })
    const running = scriptedReply(body(prompt('[keychain]'), call('x'), result('{"status": "running"}')))
    expect(running).toEqual({ text: `${NOT_DONE}{"status": "running"}` })
  })

  it('carries the new model through BOSL2, the patch and the render', () => {
    const turn: { role: string; content: unknown }[] = [prompt('Make a rounded plate [bosl2:Rounded plate 7]')]
    const next = () => scriptedReply(body(...turn))
    const answer = (text: string) => turn.push(call('x'), result(text))

    expect(next()).toEqual({ toolUse: { name: `${TOOL_PREFIX}create_from_template`, input: { name: 'Rounded plate 7', from: 'blank' } } })
    answer('{"slug": "rounded-plate-7", "version": "aaaaaaa"}')
    expect(next()).toEqual({ toolUse: { name: `${TOOL_PREFIX}pin_library`, input: { slug: 'rounded-plate-7', name: 'BOSL2' } } })
    answer('{"slug": "rounded-plate-7", "version": "bbbbbbb", "libraries": [{"name": "BOSL2"}]}')
    expect(next()).toMatchObject({
      toolUse: { name: `${TOOL_PREFIX}apply_patch`, input: { slug: 'rounded-plate-7', base: 'bbbbbbb' } },
    })
    answer('{"slug": "rounded-plate-7", "version": "ccccccc"}')
    expect(next()).toEqual({ toolUse: { name: `${TOOL_PREFIX}render_model`, input: { slug: 'rounded-plate-7', params: {} } } })
    answer('{"status": "done"}')
    expect(next()).toEqual({ text: DONE })
  })

  it('makes the new cable label from the bundled one', () => {
    expect(scriptedReply(body(prompt('[cable-label:Desk cables]')))).toEqual({
      toolUse: { name: `${TOOL_PREFIX}create_from_template`, input: { name: 'Desk cables', from: 'builtin:cable-label' } },
    })
  })

  it('stops when a result lacks what the next call needs', () => {
    const reply = scriptedReply(body(prompt('[cable-label:x]'), call('x'), result('{"slug": "x"}'), call('x'), result('{"status": "done"}')))
    expect(reply).toEqual({ text: DONE })
    expect(scriptedReply(body(prompt('[bosl2:x]'), call('x'), result('{"slug": "x"}'), call('x'), result('{"x": "BOSL2"}')))).toEqual({
      text: expect.stringContaining(`${NOT_DONE}no "version"`) as string,
    })
  })

  it('runs only the latest marked turn of a session', () => {
    const reply = scriptedReply(body(prompt('[outward]'), call('x'), result('{}'), { role: 'assistant', content: 'Done.' }, prompt('[keychain]')))
    expect(reply).toMatchObject({ toolUse: { name: `${TOOL_PREFIX}render_model` } })
  })

  it("patches text the blank template still has, so a template edit fails here and not only in the opt-in e2e", () => {
    const created = { text: '{"slug": "plate"}', isError: false }
    const pinned = { text: '{"version": "v1"}', isError: false }
    const patch = SCENARIOS.bosl2!.find((step) => step.tool === 'apply_patch')!
    const { edits } = patch.input([created, pinned], 'Plate') as { edits: { search: string }[] }
    expect(edits.length).toBeGreaterThan(0)
    for (const { search } of edits) expect(BLANK_TEMPLATE).toContain(search)
  })
})
