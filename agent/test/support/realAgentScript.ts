import type { MessagesBody, Reply } from './fakeAnthropic.js'

// The script `frontend/e2e/real-agent.spec.ts` drives the real agent with (#1923):
// what the "model" says to each /v1/messages request, decided from the request
// alone, so one long-running endpoint serves every test in that file, in any order
// and against any number of sessions. `serveScriptedModel.ts` serves it.
//
// A prompt picks a scenario with a marker, `[name]` or `[name:argument]`, and the
// scenario is a fixed list of tool calls. The Nth call is made once N-1 results have
// come back; each result must match its step's `expect`, or the turn ends with
// "Not done: <the result>". After the last step the reply is "Done.", so "Done." in
// the panel means every call the scenario made succeeded against the real backend.
// A prompt with no marker gets "Hello from the fake model.".
//
// A tool call is only made in a request that offers that tool. Claude Code also asks
// the endpoint for things that are not the turn (a title, say), and those requests
// carry the prompt, marker included, but no ScadBuddy tools: they get text.

/** How Claude Code names a tool of the agent's own `scadbuddy` MCP server. */
export const TOOL_PREFIX = 'mcp__scadbuddy__'

export const HELLO = 'Hello from the fake model.'
export const DONE = 'Done.'
export const NOT_DONE = 'Not done: '

/** The bundled keychain, as the backend names a built-in. */
export const KEYCHAIN = 'builtin:name-keychain'

type Result = { text: string; isError: boolean }

type Step = {
  tool: string
  input: (results: Result[], argument: string) => Record<string, unknown>
  /** What a successful result says; anything else ends the turn as not done. */
  expect?: RegExp
}

/** A rendered job, as `render_model` answers it. */
const RENDERED = /"status":\s*"done"/

function field(result: Result | undefined, name: string): string {
  const match = result?.text.match(new RegExp(`"${name}":\\s*"([^"]+)"`))
  if (!match?.[1]) throw new Error(`no "${name}" in the result: ${result?.text.slice(0, 200)}`)
  return match[1]
}

const slugOf = (results: Result[]) => field(results[0], 'slug')

// The blank template's plate (agent/src/tools/templates.ts BLANK_TEMPLATE), and the
// same plate as a BOSL2 cuboid with its vertical edges rounded.
const BLANK_PLATE = 'color(base_color)\n    cube([length, width, thickness]);'
const ROUNDED_PLATE =
  'color(base_color)\n    cuboid([length, width, thickness], rounding = 2, edges = "Z", anchor = BOTTOM + LEFT + FRONT);'

export const SCENARIOS: Record<string, Step[]> = {
  // Spec §8.2: an outward call stops at the approval card.
  outward: [{ tool: 'set_print_options', input: () => ({ scope: 'global', options: {} }) }],
  // #259: "Change the name-keychain's text and colour".
  keychain: [
    {
      tool: 'render_model',
      input: () => ({ slug: KEYCHAIN, params: { name: 'Ada', text_color: '#FF0000' } }),
      expect: RENDERED,
    },
  ],
  // #259: "Create a new cable label model" from a template, through to a render.
  // The argument is the new model's name.
  'cable-label': [
    { tool: 'create_from_template', input: (_, name) => ({ name, from: 'builtin:cable-label' }), expect: /"slug":/ },
    {
      tool: 'render_model',
      input: (results) => ({ slug: slugOf(results), params: { text: 'USB-C' } }),
      expect: RENDERED,
    },
  ],
  // #259: "Add BOSL2 and use a rounded cube". The argument is the new model's name.
  bosl2: [
    { tool: 'create_from_template', input: (_, name) => ({ name, from: 'blank' }), expect: /"slug":/ },
    { tool: 'pin_library', input: (results) => ({ slug: slugOf(results), name: 'BOSL2' }), expect: /"BOSL2"/ },
    {
      tool: 'apply_patch',
      input: (results) => ({
        slug: slugOf(results),
        base: field(results[1], 'version'),
        edits: [
          { search: '/* [Hidden] */\n', replace: '/* [Hidden] */\n\ninclude <BOSL2/std.scad>\n' },
          { search: BLANK_PLATE, replace: ROUNDED_PLATE },
        ],
        message: 'Round the plate with BOSL2',
      }),
      expect: /"version":/,
    },
    { tool: 'render_model', input: (results) => ({ slug: slugOf(results), params: {} }), expect: RENDERED },
  ],
}

type Block = { type?: string; text?: string; content?: unknown; is_error?: boolean }

function blocks(content: unknown): Block[] {
  if (typeof content === 'string') return [{ type: 'text', text: content }]
  return Array.isArray(content) ? (content as Block[]) : []
}

function textOf(content: unknown): string {
  return blocks(content)
    .map((block) => (block.type === 'text' ? (block.text ?? '') : ''))
    .join('\n')
}

type Turn = { name: string; argument: string; results: Result[] }

const MARKER = /\[([a-z0-9-]+)(?::([^\]]+))?\]/g

/**
 * The scenario the turn runs and the results it has had so far. The turn starts at the
 * last user message that carries no tool result and names a scenario, `[name]` or
 * `[name:argument]`; the tool results after it are its own. An earlier turn of the same
 * session, with its marker and results, is history and plays no part.
 */
export function turnOf(body: MessagesBody | undefined): Turn | undefined {
  const messages = body?.messages ?? []
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index]
    if (message?.role !== 'user' || blocks(message.content).some((block) => block.type === 'tool_result')) continue
    const known = [...textOf(message.content).matchAll(MARKER)].find((match) => match[1]! in SCENARIOS)
    if (!known) continue
    return { name: known[1]!, argument: known[2]?.trim() ?? '', results: resultsOf(messages.slice(index + 1)) }
  }
  return undefined
}

function resultsOf(messages: { content: unknown }[]): Result[] {
  return messages.flatMap((message) =>
    blocks(message.content)
      .filter((block) => block.type === 'tool_result')
      .map((block) => ({ text: textOf(block.content), isError: block.is_error === true })),
  )
}

export function scriptedReply(body: MessagesBody | undefined): Reply {
  const turn = turnOf(body)
  if (!turn) return { text: resultsOf(body?.messages ?? []).length > 0 ? DONE : HELLO }
  const { results, argument } = turn
  const steps = SCENARIOS[turn.name] ?? []
  for (const [index, result] of results.entries()) {
    const expected = steps[index]?.expect
    if (result.isError || (expected && !expected.test(result.text))) {
      return { text: `${NOT_DONE}${result.text.slice(0, 500)}` }
    }
  }
  const next = steps[results.length]
  if (!next) return { text: DONE }
  const name = `${TOOL_PREFIX}${next.tool}`
  if (!body?.tools?.some((tool) => tool.name === name)) return { text: HELLO }
  try {
    return { toolUse: { name, input: next.input(results, argument) } }
  } catch (err) {
    return { text: `${NOT_DONE}${(err as Error).message}` }
  }
}
